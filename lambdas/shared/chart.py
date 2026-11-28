"""Shape a patient's FHIR resources into EHR-style chart sections for the Start page.

Sections: problems, medications, vitals and labs (latest value per test), encounters, notes.
Each item says where it came from: "ai" (written by this app from an AI draft), "clinician"
(added by hand at review) or "record" (pre-existing data, e.g. the loaded sample history).
"""

from shared.review import patient_summary

SYSTEMS = {
    "http://snomed.info/sct": "SNOMED CT",
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD-10-CM",
    "http://www.nlm.nih.gov/research/umls/rxnorm": "RxNorm",
    "http://loinc.org": "LOINC",
}
MAX_ENCOUNTERS = 10
MAX_NOTES = 10


def _concept(concept: dict | None) -> dict:
    concept = concept or {}
    coding = (concept.get("coding") or [{}])[0]
    return {
        "display": concept.get("text") or coding.get("display") or coding.get("code") or "Unknown",
        "code": coding.get("code"),
        "system": SYSTEMS.get(coding.get("system"), coding.get("system")),
    }


def _code(concept: dict | None) -> str | None:
    return ((concept or {}).get("coding") or [{}])[0].get("code")


def _origin(resource: dict) -> str:
    meta = resource.get("meta", {})
    if any(s.get("code") == "AIAST" for s in meta.get("security", [])):
        return "ai"
    if any(t.get("code") == "clinician-entered" for t in meta.get("tag", [])):
        return "clinician"
    return "record"


def _day(value: str | None) -> str | None:
    return value[:10] if value else None


def _value(obs: dict) -> str:
    if "valueQuantity" in obs:
        q = obs["valueQuantity"]
        return f"{q.get('value')} {q.get('unit') or q.get('code') or ''}".strip()
    if "valueString" in obs:
        return obs["valueString"]
    if "valueCodeableConcept" in obs:
        return _concept(obs["valueCodeableConcept"])["display"]
    return "–"


def _newest_first(items: list[dict], key: str = "date") -> list[dict]:
    return sorted(items, key=lambda i: i.get(key) or "", reverse=True)


def _problems(conditions: list[dict]) -> list[dict]:
    rows = [{
        **_concept(c.get("code")),
        "clinicalStatus": _code(c.get("clinicalStatus")),
        "verificationStatus": _code(c.get("verificationStatus")),
        "date": _day(c.get("onsetDateTime") or c.get("recordedDate")),
        "origin": _origin(c),
    } for c in conditions]
    rows = _newest_first(rows)
    return sorted(rows, key=lambda r: r["clinicalStatus"] != "active")  # stable: active first, newest first


def _medications(requests: list[dict], statements: list[dict]) -> list[dict]:
    rows = [{**_concept(m.get("medicationCodeableConcept")), "status": m.get("status"), "date": _day(m.get("authoredOn")),
             "kind": "Prescribed", "origin": _origin(m)} for m in requests]
    rows += [{**_concept(m.get("medicationCodeableConcept")), "status": m.get("status"),
              "date": _day(m.get("dateAsserted") or m.get("effectiveDateTime")),
              "kind": "Reported", "origin": _origin(m)} for m in statements]
    return sorted(_newest_first(rows), key=lambda r: r["status"] != "active")


def _latest_observations(observations: list[dict], category: str) -> list[dict]:
    latest: dict[str, dict] = {}
    for obs in observations:
        categories = {_code(c) for c in obs.get("category", [])}
        if category not in categories:
            continue
        row = {**_concept(obs.get("code")), "value": _value(obs), "date": _day(obs.get("effectiveDateTime")),
               "origin": _origin(obs)}
        key = row["code"] or row["display"]
        if key not in latest or (row["date"] or "") > (latest[key]["date"] or ""):
            latest[key] = row
    return sorted(latest.values(), key=lambda r: r["display"].lower())


def _encounters(encounters: list[dict]) -> list[dict]:
    rows = []
    for e in encounters:
        kind = (e.get("type") or [{}])[0]
        start = (e.get("period") or {}).get("start")
        rows.append({"id": e.get("id"), "start": start, "end": (e.get("period") or {}).get("end"),
                     "date": _day(start), "type": _concept(kind)["display"],
                     "class": (e.get("class") or {}).get("display") or (e.get("class") or {}).get("code"),
                     "status": e.get("status")})
    return sorted(rows, key=lambda r: r["start"] or "", reverse=True)[:MAX_ENCOUNTERS]


def _notes(documents: list[dict]) -> list[dict]:
    rows = [{"date": _day(d.get("date")), "title": (d.get("type") or {}).get("text") or d.get("description") or "Note",
             "status": d.get("status"), "origin": _origin(d)} for d in documents]
    return _newest_first(rows)[:MAX_NOTES]


def build_chart(patient: dict, resources: dict[str, list[dict]], today) -> dict:
    conditions = resources.get("Condition", [])
    active = [c for c in conditions if _code(c.get("clinicalStatus")) == "active"]
    observations = resources.get("Observation", [])
    return {
        "patient": patient_summary(patient, active, today),
        "problems": _problems(conditions),
        "medications": _medications(resources.get("MedicationRequest", []), resources.get("MedicationStatement", [])),
        "vitals": _latest_observations(observations, "vital-signs"),
        "labs": _latest_observations(observations, "laboratory"),
        "encounters": _encounters(resources.get("Encounter", [])),
        "notes": _notes(resources.get("DocumentReference", [])),
    }
