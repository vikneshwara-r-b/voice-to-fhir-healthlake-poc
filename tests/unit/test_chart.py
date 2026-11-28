"""EHR-style chart sections built from FHIR search results (synthetic data)."""

from datetime import date

from shared.chart import build_chart

PATIENT = {"resourceType": "Patient", "id": "p-1", "gender": "female", "birthDate": "1962-07-15",
           "name": [{"given": ["Test"], "family": "Patient"}]}


def _status(system, code):
    return {"coding": [{"system": f"http://terminology.hl7.org/CodeSystem/{system}", "code": code}]}


def condition(text, code, onset, status="active", meta=None):
    c = {"resourceType": "Condition", "clinicalStatus": _status("condition-clinical", status),
         "verificationStatus": _status("condition-ver-status", "confirmed"),
         "code": {"coding": [{"system": "http://snomed.info/sct", "code": code}], "text": text},
         "onsetDateTime": onset}
    if meta:
        c["meta"] = meta
    return c


def obs(category, code, display, value, unit, when):
    return {"resourceType": "Observation", "category": [{"coding": [{"code": category}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": code, "display": display}], "text": display},
            "valueQuantity": {"value": value, "unit": unit}, "effectiveDateTime": when}


def test_sections_are_grouped_sorted_and_labelled():
    resources = {
        "Condition": [
            condition("Hyperlipidemia", "55822004", "2025-07-18"),
            condition("Contusion", "125668008", "2025-05-26", status="resolved"),
            condition("Essential hypertension", "I10", None,
                      meta={"security": [{"code": "AIAST",
                                          "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue"}]}),
        ],
        "MedicationRequest": [{"medicationCodeableConcept": {"text": "Atorvastatin 10 MG"}, "status": "active",
                               "authoredOn": "2025-07-18"}],
        "MedicationStatement": [{"medicationCodeableConcept": {"text": "metformin 500 MG"}, "status": "active",
                                 "dateAsserted": "2026-11-27T10:05:00Z",
                                 "meta": {"tag": [{"code": "clinician-entered"}]}}],
        "Observation": [
            obs("vital-signs", "8480-6", "Systolic blood pressure", 139, "mmHg", "2024-07-20"),
            obs("vital-signs", "8480-6", "Systolic blood pressure", 145, "mmHg", "2025-11-01"),
            obs("laboratory", "718-7", "Hemoglobin", 15.4, "g/dL", "2025-07-19"),
        ],
        "Encounter": [{"status": "finished", "class": {"code": "AMB", "display": "ambulatory"},
                       "type": [{"text": "Sleep study"}], "period": {"start": "2024-09-20T09:00:00Z"}},
                      {"status": "finished", "class": {"code": "AMB"}, "type": [{"text": "Urgent care"}],
                       "period": {"start": "2025-11-01T09:00:00Z"}}],
        "DocumentReference": [{"status": "current", "date": "2026-11-27T10:00:00Z",
                               "type": {"text": "Consultation transcript (AI-generated)"},
                               "meta": {"security": [{"code": "AIAST"}]}}],
    }
    chart = build_chart(PATIENT, resources, date(2026, 11, 27))

    assert chart["patient"]["age"] == 64
    assert chart["patient"]["activeConditions"] == ["Essential hypertension", "Hyperlipidemia"]
    assert [(p["display"], p["clinicalStatus"], p["origin"]) for p in chart["problems"]] == [
        ("Hyperlipidemia", "active", "record"), ("Essential hypertension", "active", "ai"),
        ("Contusion", "resolved", "record")]
    assert chart["problems"][0]["system"] == "SNOMED CT"
    assert [(m["display"], m["kind"], m["origin"]) for m in chart["medications"]] == [
        ("metformin 500 MG", "Reported", "clinician"), ("Atorvastatin 10 MG", "Prescribed", "record")]
    assert chart["vitals"] == [{"display": "Systolic blood pressure", "code": "8480-6", "system": "LOINC",
                                "value": "145 mmHg", "date": "2025-11-01", "origin": "record"}]
    assert [l["display"] for l in chart["labs"]] == ["Hemoglobin"]
    assert [e["type"] for e in chart["encounters"]] == ["Urgent care", "Sleep study"]
    assert chart["notes"][0] == {"date": "2026-11-27", "title": "Consultation transcript (AI-generated)",
                                 "status": "current", "origin": "ai"}


def test_empty_patient_has_empty_sections():
    chart = build_chart(PATIENT, {}, date(2026, 11, 27))
    assert all(chart[k] == [] for k in ("problems", "medications", "vitals", "labs", "encounters", "notes"))
