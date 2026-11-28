"""Map Comprehend Medical output to draft FHIR R4 resources.

Rules (also in the README):
- DocumentReference: the labelled transcript as base64 text/plain, LOINC 11506-3.
- Condition: InferICD10CM MEDICAL_CONDITION entities with a DIAGNOSIS, SIGN or
  SYMPTOM trait and at least one ICD-10-CM code. verificationStatus is
  "provisional", or "differential" when the entity is HYPOTHETICAL.
- MedicationStatement: InferRxNorm MEDICATION entities with an RxNorm code;
  status "completed" for PAST_HISTORY, otherwise "active".
- Excluded (shown separately, never written): NEGATION, PERTAINS_TO_FAMILY,
  conditions without a diagnosis/sign/symptom trait, entities with no code.
- Flagged (shown, not pre-selected): HYPOTHETICAL or LOW_CONFIDENCE trait,
  entity score below the threshold (default 0.80), or top code score below the
  code floor (default 0.30). Code scores rank candidate codes and are typically
  0.3-0.6 even for correct codes, so they get a lower bar; they are still shown.
- Each code is proposed once: the unflagged, highest-confidence mention wins.
  Repeated excluded mentions are listed once with a mention count.
- Every resource carries meta.security AIAST and a confidence extension.
"""

import base64

from shared.scribe import TEMPLATES, note_text
from shared.transcript_parser import labelled_transcript, transcript_confidence

AI_ASSERTED = {
    "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue",
    "code": "AIAST",
    "display": "Artificial Intelligence asserted",
}
CONFIDENCE_EXTENSION_URL = "http://example.org/fhir/StructureDefinition/ai-confidence"
ICD10CM_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
RXNORM_SYSTEM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LOINC_SYSTEM = "http://loinc.org"
MAX_NOTE_CHARS = 10_000  # HealthLake quota: characters in a medical note

CONDITION_TRAITS = {"DIAGNOSIS", "SIGN", "SYMPTOM"}
DOSAGE_ATTRIBUTES = ("STRENGTH", "DOSAGE", "FORM", "ROUTE_OR_MODE", "FREQUENCY", "DURATION", "RATE")


def _traits(entity: dict) -> set[str]:
    return {t["Name"] for t in entity.get("Traits", [])}


def _ai_fields(confidence: float | None) -> dict:
    """AI-asserted security label, plus a confidence extension when one is known
    (FHIR forbids empty arrays, so no extension key at all otherwise)."""
    fields = {"meta": {"security": [dict(AI_ASSERTED)]}}
    if confidence is not None:
        fields["extension"] = [{"url": CONFIDENCE_EXTENSION_URL, "valueDecimal": round(confidence, 3)}]
    return fields


def _codeable(system: str, concept: dict, text: str) -> dict:
    return {
        "coding": [{"system": system, "code": concept["Code"], "display": concept["Description"]}],
        "text": text,
    }


def _assess(entity: dict, concept: dict, ctx: dict) -> tuple[list[str], float, float]:
    """Return (flag reasons, entity score, code score)."""
    traits = _traits(entity)
    entity_score, code_score = entity.get("Score", 0), concept.get("Score", 0)
    reasons = [t for t in ("HYPOTHETICAL", "LOW_CONFIDENCE") if t in traits]
    if entity_score < ctx["threshold"]:
        reasons.append("LOW_ENTITY_SCORE")
    if code_score < ctx["codeFloor"]:
        reasons.append("LOW_CODE_SCORE")
    return reasons, entity_score, code_score


def _excluded(entity: dict, source: str, reason: str) -> dict:
    return {
        "source": source,
        "text": entity.get("Text", ""),
        "category": entity.get("Category"),
        "reason": reason,
        "score": round(entity.get("Score", 0), 3),
        "turnIndex": entity.get("TurnIndex"),
    }


def _candidate(kind, entity, concept, reasons, entity_score, code_score, resource) -> dict:
    return {
        "resourceType": kind,
        "code": concept["Code"],
        "label": concept["Description"],
        "sourceText": entity.get("Text", ""),
        "turnIndex": entity.get("TurnIndex"),
        "flagged": bool(reasons),
        "flagReasons": reasons,
        "selected": not reasons,
        "confidence": round(entity_score, 3),
        "codeScore": round(code_score, 3),
        "resource": resource,
    }


def map_conditions(entities, ctx) -> tuple[list[dict], list[dict]]:
    proposed, excluded = [], []
    for entity in entities:
        if entity.get("Category") != "MEDICAL_CONDITION":
            continue
        traits = _traits(entity)
        if "NEGATION" in traits:
            excluded.append(_excluded(entity, "ICD10CM", "NEGATION"))
            continue
        if "PERTAINS_TO_FAMILY" in traits:
            excluded.append(_excluded(entity, "ICD10CM", "FAMILY_HISTORY"))
            continue
        if not traits & CONDITION_TRAITS:
            excluded.append(_excluded(entity, "ICD10CM", "NOT_DIAGNOSIS_SIGN_OR_SYMPTOM"))
            continue
        concepts = sorted(entity.get("ICD10CMConcepts", []), key=lambda c: -c.get("Score", 0))
        if not concepts:
            excluded.append(_excluded(entity, "ICD10CM", "NO_CODE"))
            continue
        concept = concepts[0]
        reasons, entity_score, code_score = _assess(entity, concept, ctx)
        verification = "differential" if "HYPOTHETICAL" in traits else "provisional"
        resource = {
            "resourceType": "Condition",
            **_ai_fields(entity_score),
            "clinicalStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]},
            "verificationStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": verification}]},
            "category": [{"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                "code": "encounter-diagnosis", "display": "Encounter Diagnosis"}]}],
            "code": _codeable(ICD10CM_SYSTEM, concept, entity.get("Text", "")),
            "subject": {"reference": f"Patient/{ctx['patientId']}"},
            "encounter": {"reference": f"Encounter/{ctx['encounterId']}"},
            "recordedDate": ctx["now"],
        }
        proposed.append(_candidate("Condition", entity, concept, reasons, entity_score, code_score, resource))
    return proposed, excluded


def map_medications(entities, ctx) -> tuple[list[dict], list[dict]]:
    proposed, excluded = [], []
    for entity in entities:
        if entity.get("Category") != "MEDICATION":
            continue
        traits = _traits(entity)
        if "NEGATION" in traits:
            excluded.append(_excluded(entity, "RXNORM", "NEGATION"))
            continue
        concepts = sorted(entity.get("RxNormConcepts", []), key=lambda c: -c.get("Score", 0))
        if not concepts:
            excluded.append(_excluded(entity, "RXNORM", "NO_CODE"))
            continue
        concept = concepts[0]
        reasons, entity_score, code_score = _assess(entity, concept, ctx)
        resource = {
            "resourceType": "MedicationStatement",
            **_ai_fields(entity_score),
            "status": "completed" if "PAST_HISTORY" in traits else "active",
            "medicationCodeableConcept": _codeable(RXNORM_SYSTEM, concept, entity.get("Text", "")),
            "subject": {"reference": f"Patient/{ctx['patientId']}"},
            "context": {"reference": f"Encounter/{ctx['encounterId']}"},
            "dateAsserted": ctx["now"],
        }
        dosage = " ".join(
            a["Text"] for a in sorted(entity.get("Attributes", []), key=lambda a: a.get("BeginOffset", 0))
            if a.get("Type") in DOSAGE_ATTRIBUTES and a.get("Text")
        )
        if dosage:
            resource["dosage"] = [{"text": dosage}]
        proposed.append(_candidate("MedicationStatement", entity, concept, reasons, entity_score, code_score, resource))
    return proposed, excluded


def _dedupe_excluded(items: list[dict]) -> list[dict]:
    """Collapse repeated mentions (same source, reason and text) into one entry with a count."""
    merged: dict[tuple, dict] = {}
    for item in items:
        key = (item["source"], item["reason"], item["text"].lower())
        if key in merged:
            merged[key]["mentions"] += 1
            merged[key]["score"] = max(merged[key]["score"], item["score"])
        else:
            merged[key] = {**item, "mentions": 1}
    return list(merged.values())


def _dedupe(candidates: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for c in candidates:
        current = best.get(c["code"])
        if current is None or (c["flagged"], -c["confidence"]) < (current["flagged"], -current["confidence"]):
            best[c["code"]] = c
    return list(best.values())


def document_reference(turns, ctx) -> dict:
    text = labelled_transcript(turns)
    confidence = transcript_confidence(turns)
    reasons = ["TRANSCRIPT_OVER_10000_CHARS"] if len(text) > MAX_NOTE_CHARS else []
    resource = {
        "resourceType": "DocumentReference",
        **_ai_fields(confidence),
        "status": "current",
        "type": {
            "coding": [{"system": LOINC_SYSTEM, "code": "11506-3", "display": "Progress note"}],
            "text": "Consultation transcript (AI-generated)",
        },
        "subject": {"reference": f"Patient/{ctx['patientId']}"},
        "date": ctx["now"],
        "description": f"Transcript of a synthetic primary-care consultation ({ctx.get('transcriptSource') or 'Amazon Transcribe Medical'})",
        "content": [{"attachment": {
            "contentType": "text/plain",
            "language": "en-US",
            "title": "Consultation transcript",
            "creation": ctx["now"],
            "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }}],
        "context": {"encounter": [{"reference": f"Encounter/{ctx['encounterId']}"}]},
    }
    return {
        "resourceType": "DocumentReference",
        "code": "11506-3",
        "label": "Consultation transcript",
        "sourceText": None,
        "turnIndex": None,
        "flagged": bool(reasons),
        "flagReasons": reasons,
        "selected": not reasons,
        "confidence": confidence,
        "codeScore": None,
        "resource": resource,
    }


def note_reference(note: dict, ctx) -> dict:
    """The HealthScribe clinical note as a DocumentReference. Generative output: flagged, not pre-selected."""
    title, loinc, display = TEMPLATES.get(note["template"], (note["template"], "11506-3", "Progress note"))
    text = note_text(note)
    reasons = ["AI_GENERATED_NOTE"] + (["TRANSCRIPT_OVER_10000_CHARS"] if len(text) > MAX_NOTE_CHARS else [])
    resource = {
        "resourceType": "DocumentReference",
        **_ai_fields(None),
        "status": "current",
        "type": {"coding": [{"system": LOINC_SYSTEM, "code": loinc, "display": display}],
                 "text": f"{title} note (AI-generated by AWS HealthScribe)"},
        "subject": {"reference": f"Patient/{ctx['patientId']}"},
        "date": ctx["now"],
        "description": "Clinical note drafted by AWS HealthScribe from a synthetic consultation",
        "content": [{"attachment": {
            "contentType": "text/plain", "language": "en-US", "title": f"{title} note", "creation": ctx["now"],
            "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }}],
        "context": {"encounter": [{"reference": f"Encounter/{ctx['encounterId']}"}]},
    }
    return {"resourceType": "DocumentReference", "code": loinc, "label": f"Clinical note: {title}",
            "sourceText": None, "turnIndex": None, "flagged": True, "flagReasons": reasons, "selected": False,
            "confidence": None, "codeScore": None, "noteTemplate": note["template"], "resource": resource}


def build_draft(*, draft_id, patient_id, encounter_id, turns, nlp, now, threshold=0.80, code_floor=0.30,
                note=None, transcript_source=None) -> dict:
    ctx = {"patientId": patient_id, "encounterId": encounter_id, "now": now,
           "threshold": threshold, "codeFloor": code_floor, "transcriptSource": transcript_source}
    conditions, excluded_conditions = map_conditions(nlp["icd10cm"], ctx)
    medications, excluded_medications = map_medications(nlp["rxnorm"], ctx)

    proposed = [document_reference(turns, ctx), *([note_reference(note, ctx)] if note else []),
                *_dedupe(conditions), *_dedupe(medications)]
    counters: dict[str, int] = {}
    for item in proposed:
        prefix = item["resourceType"].lower()
        counters[prefix] = counters.get(prefix, 0) + 1
        item["id"] = f"{prefix}-{counters[prefix]}"

    return {
        "draftId": draft_id,
        "patientId": patient_id,
        "encounterId": encounter_id,
        "createdAt": now,
        "confidenceThreshold": threshold,
        "codeScoreFloor": code_floor,
        "turns": turns,
        "proposed": proposed,
        "excluded": _dedupe_excluded(excluded_conditions + excluded_medications),
    }
