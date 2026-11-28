import base64

from conftest import cm_entity
from shared.fhir_mapper import AI_ASSERTED, CONFIDENCE_EXTENSION_URL, build_draft
from shared.fhir_validate import validate

TURNS = [
    {"index": 0, "speaker": "spk_0", "role": "Clinician", "text": "Any chest pain?", "confidence": 0.98},
    {"index": 1, "speaker": "spk_1", "role": "Patient", "text": "No chest pain.", "confidence": 0.96},
]
HTN = [("I10", "Essential (primary) hypertension", 0.92)]


def _draft(icd10=(), rxnorm=()):
    return build_draft(
        draft_id="d-1", patient_id="patient-1", encounter_id="enc-1", turns=TURNS,
        nlp={"icd10cm": list(icd10), "rxnorm": list(rxnorm)}, now="2026-11-27T10:00:00Z",
    )


def _by_type(draft, kind):
    return [p for p in draft["proposed"] if p["resourceType"] == kind]


def test_asserted_condition_is_proposed_and_preselected():
    draft = _draft([cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], codes=HTN)])
    [cond] = _by_type(draft, "Condition")
    assert cond["selected"] and not cond["flagged"]
    assert cond["code"] == "I10"
    res = cond["resource"]
    assert res["code"]["coding"][0] == {
        "system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "I10", "display": "Essential (primary) hypertension"}
    assert res["verificationStatus"]["coding"][0]["code"] == "provisional"
    assert res["subject"] == {"reference": "Patient/patient-1"}
    assert res["encounter"] == {"reference": "Encounter/enc-1"}
    assert validate(res) == []


def test_negated_condition_is_excluded():
    draft = _draft([cm_entity("chest pain", "MEDICAL_CONDITION", traits=["SYMPTOM", "NEGATION"],
                              codes=[("R07.9", "Chest pain, unspecified", 0.9)])])
    assert _by_type(draft, "Condition") == []
    assert [(e["reason"], e["text"]) for e in draft["excluded"]] == [("NEGATION", "chest pain")]


def test_family_history_condition_is_excluded():
    draft = _draft([cm_entity("heart attack", "MEDICAL_CONDITION", traits=["DIAGNOSIS", "PERTAINS_TO_FAMILY"],
                              codes=[("I21.9", "Acute myocardial infarction", 0.9)])])
    assert _by_type(draft, "Condition") == []
    assert draft["excluded"][0]["reason"] == "FAMILY_HISTORY"


def test_hypothetical_condition_is_flagged_differential_and_not_selected():
    draft = _draft([cm_entity("anemia", "MEDICAL_CONDITION", traits=["DIAGNOSIS", "HYPOTHETICAL"],
                              codes=[("D64.9", "Anemia, unspecified", 0.9)])])
    [cond] = _by_type(draft, "Condition")
    assert cond["flagged"] and not cond["selected"]
    assert cond["flagReasons"] == ["HYPOTHETICAL"]
    assert cond["resource"]["verificationStatus"]["coding"][0]["code"] == "differential"


def test_entity_score_threshold_and_code_score_floor():
    draft = _draft([
        cm_entity("knee pain", "MEDICAL_CONDITION", traits=["SYMPTOM"], score=0.79,
                  codes=[("M25.561", "Pain in right knee", 0.95)]),
        cm_entity("osteoarthritis", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], score=0.99,
                  codes=[("M17.9", "Osteoarthritis of knee, unspecified", 0.29)]),
        cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], score=0.9,
                  codes=[("I10", "Essential (primary) hypertension", 0.45)]),
    ])
    reasons = {c["code"]: c["flagReasons"] for c in _by_type(draft, "Condition")}
    # Entity bar 0.80; code scores only flag below the 0.30 floor (0.45 is fine).
    assert reasons == {"M25.561": ["LOW_ENTITY_SCORE"], "M17.9": ["LOW_CODE_SCORE"], "I10": []}
    selected = {c["code"]: c["selected"] for c in _by_type(draft, "Condition")}
    assert selected == {"M25.561": False, "M17.9": False, "I10": True}


def test_low_confidence_trait_is_flagged():
    draft = _draft([cm_entity("cough", "MEDICAL_CONDITION", traits=["SYMPTOM", "LOW_CONFIDENCE"],
                              codes=[("R05.9", "Cough, unspecified", 0.9)])])
    assert _by_type(draft, "Condition")[0]["flagReasons"] == ["LOW_CONFIDENCE"]


def test_condition_without_diagnosis_sign_symptom_or_code_is_excluded():
    draft = _draft([
        cm_entity("tired", "MEDICAL_CONDITION", traits=[], codes=HTN),
        cm_entity("dizzy", "MEDICAL_CONDITION", traits=["SYMPTOM"], codes=[]),
    ])
    assert _by_type(draft, "Condition") == []
    assert sorted(e["reason"] for e in draft["excluded"]) == ["NOT_DIAGNOSIS_SIGN_OR_SYMPTOM", "NO_CODE"]


def test_duplicate_codes_keep_the_unflagged_best_mention():
    draft = _draft([
        cm_entity("high blood pressure", "MEDICAL_CONDITION", traits=["DIAGNOSIS", "HYPOTHETICAL"], codes=HTN),
        cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], score=0.85, codes=HTN),
    ])
    [cond] = _by_type(draft, "Condition")
    assert cond["sourceText"] == "hypertension" and not cond["flagged"]


def test_medications_status_dosage_and_negation():
    draft = _draft(rxnorm=[
        cm_entity("amlodipine", "MEDICATION", codes=[("197361", "amlodipine 5 MG Oral Tablet", 0.9)],
                  attributes=[{"Type": "DOSAGE", "Text": "five milligrams", "BeginOffset": 11},
                              {"Type": "FREQUENCY", "Text": "once a day", "BeginOffset": 27}]),
        cm_entity("colchicine", "MEDICATION", traits=["PAST_HISTORY"],
                  codes=[("197541", "colchicine 0.6 MG Oral Tablet", 0.9)]),
        cm_entity("antibiotic", "MEDICATION", traits=["NEGATION"], codes=[("1", "x", 0.9)]),
    ])
    meds = {m["code"]: m["resource"] for m in _by_type(draft, "MedicationStatement")}
    assert meds["197361"]["status"] == "active"
    assert meds["197361"]["dosage"] == [{"text": "five milligrams once a day"}]
    assert meds["197541"]["status"] == "completed"
    assert "dosage" not in meds["197541"]
    assert [e["reason"] for e in draft["excluded"]] == ["NEGATION"]
    assert all(validate(r) == [] for r in meds.values())


def test_document_reference_holds_labelled_transcript():
    [doc] = _by_type(_draft(), "DocumentReference")
    res = doc["resource"]
    assert doc["selected"] and not doc["flagged"]
    assert res["status"] == "current"
    assert res["type"]["coding"][0] == {"system": "http://loinc.org", "code": "11506-3", "display": "Progress note"}
    text = base64.b64decode(res["content"][0]["attachment"]["data"]).decode()
    assert text == "Clinician: Any chest pain?\nPatient: No chest pain."
    assert res["context"]["encounter"] == [{"reference": "Encounter/enc-1"}]
    assert validate(res) == []


def test_every_resource_is_ai_labelled_with_confidence_and_unique_id():
    draft = _draft(
        [cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], codes=HTN)],
        [cm_entity("amlodipine", "MEDICATION", codes=[("197361", "amlodipine 5 MG Oral Tablet", 0.9)])],
    )
    assert [p["id"] for p in draft["proposed"]] == ["documentreference-1", "condition-1", "medicationstatement-1"]
    for item in draft["proposed"]:
        res = item["resource"]
        assert res["meta"]["security"] == [AI_ASSERTED]
        assert res["extension"][0]["url"] == CONFIDENCE_EXTENSION_URL
        assert 0 <= res["extension"][0]["valueDecimal"] <= 1


def test_validator_catches_broken_resources():
    [cond] = _by_type(_draft([cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], codes=HTN)]),
                      "Condition")
    broken = dict(cond["resource"], subject={"reference": "Group/1"}, meta={"security": []}, extension=[])
    problems = validate(broken)
    assert "subject must reference Patient/{id}" in problems
    assert "missing AIAST security label" in problems
    assert any(p.startswith("empty value") for p in problems)


def test_repeated_excluded_mentions_are_listed_once_with_a_count():
    fever = [("R50.9", "Fever, unspecified", 0.3)]
    draft = _draft([
        cm_entity("fever", "MEDICAL_CONDITION", traits=["SYMPTOM", "NEGATION"], score=0.9, codes=fever),
        cm_entity("Fever", "MEDICAL_CONDITION", traits=["SYMPTOM", "NEGATION"], score=0.96, codes=fever),
    ])
    [item] = draft["excluded"]
    assert (item["reason"], item["mentions"], item["score"]) == ("NEGATION", 2, 0.96)
