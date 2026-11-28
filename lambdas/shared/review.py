"""Shape stored NLP output and FHIR Patient data for the review UI."""

from datetime import date

_CODED = (("icd10cm", "ICD10CMConcepts", "ICD-10-CM"), ("rxnorm", "RxNormConcepts", "RxNorm"))
# Categories the coded calls already cover; DetectEntitiesV2 adds the rest (anatomy, tests, ...).
_COVERED = {"MEDICAL_CONDITION", "MEDICATION"}


def _row(entity: dict, source: str, concept: dict | None = None, system: str | None = None) -> dict:
    return {
        "source": source,
        "text": entity.get("Text", ""),
        "category": entity.get("Category"),
        "type": entity.get("Type"),
        "traits": sorted(({"name": t["Name"], "score": round(t.get("Score", 0), 3)} for t in entity.get("Traits", [])),
                         key=lambda t: t["name"]),
        "score": round(entity.get("Score", 0), 3),
        "turnIndex": entity.get("TurnIndex"),
        "codeSystem": system if concept else None,
        "code": concept.get("Code") if concept else None,
        "codeDescription": concept.get("Description") if concept else None,
        "codeScore": round(concept.get("Score", 0), 3) if concept else None,
    }


def entity_rows(nlp: dict) -> list[dict]:
    """One row per entity: coded entities from InferICD10CM/InferRxNorm, other categories
    from DetectEntitiesV2. Sorted by position in the transcript."""
    rows = []
    for field, concept_key, system in _CODED:
        for entity in nlp.get(field, []):
            if entity.get("Category") == "TIME_EXPRESSION":
                continue
            concepts = sorted(entity.get(concept_key, []), key=lambda c: -c.get("Score", 0))
            rows.append((entity.get("BeginOffset", 0), _row(entity, field, concepts[0] if concepts else None, system)))
    for entity in nlp.get("entities", []):
        if entity.get("Category") not in _COVERED:
            rows.append((entity.get("BeginOffset", 0), _row(entity, "entities")))
    return [row for _, row in sorted(rows, key=lambda r: r[0])]


def _age(birth_date: str | None, today: date) -> int | None:
    try:
        born = date.fromisoformat(birth_date)
    except (TypeError, ValueError):
        return None
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def patient_summary(patient: dict, conditions: list[dict], today: date) -> dict:
    name = (patient.get("name") or [{}])[0]
    display = " ".join([*name.get("given", []), name.get("family", "")]).strip() or patient.get("id")
    active = sorted({
        c.get("code", {}).get("text") or (c.get("code", {}).get("coding") or [{}])[0].get("display", "")
        for c in conditions
    } - {""})
    return {
        "id": patient["id"],
        "name": display,
        "gender": patient.get("gender"),
        "birthDate": patient.get("birthDate"),
        "age": _age(patient.get("birthDate"), today),
        "activeConditions": active,
    }
