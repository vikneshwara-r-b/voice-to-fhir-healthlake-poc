"""Clinician edits: field validation, tagging, audit copy, and resources that still validate."""

import base64

import pytest

from conftest import cm_entity
from shared.draft_edit import ENTERED_TAG, EditError, add_item, edit_item, parse_feedback, set_review
from shared.fhir_mapper import AI_ASSERTED, build_draft
from shared.fhir_validate import validate


@pytest.fixture
def draft():
    return build_draft(
        draft_id="d-1", patient_id="patient-1", encounter_id="d-1",
        turns=[{"index": 0, "role": "Clinician", "text": "Hypertension. Amlodipine.", "confidence": 0.9}],
        nlp={"icd10cm": [cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"],
                                   codes=[("I10", "Essential (primary) hypertension", 0.9)])],
             "rxnorm": [cm_entity("amlodipine", "MEDICATION", codes=[("197361", "amlodipine 5 MG", 0.9)])]},
        now="2026-11-27T10:00:00Z",
    )


COND = {"code": "i10", "display": "Hypertension, poorly controlled", "clinicalStatus": "active",
        "verificationStatus": "confirmed"}


def test_edit_condition_keeps_ai_label_tags_edit_and_keeps_original(draft):
    item = edit_item(draft, "condition-1", COND)
    res = item["resource"]
    assert res["code"]["coding"][0] == {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "I10",
                                        "display": "Hypertension, poorly controlled"}
    assert res["verificationStatus"]["coding"][0]["code"] == "confirmed"
    assert res["meta"]["security"] == [AI_ASSERTED]
    assert [t["code"] for t in res["meta"]["tag"]] == ["clinician-edited"]
    assert item["edited"] and item["label"] == "Hypertension, poorly controlled"
    assert item["aiResource"]["verificationStatus"]["coding"][0]["code"] == "provisional"
    assert validate(res) == []
    # a second edit keeps the first original and does not duplicate the tag
    item = edit_item(draft, "condition-1", dict(COND, clinicalStatus="resolved"))
    assert item["aiResource"]["verificationStatus"]["coding"][0]["code"] == "provisional"
    assert len(item["resource"]["meta"]["tag"]) == 1


def test_edit_medication_status_and_dosage(draft):
    item = edit_item(draft, "medicationstatement-1",
                     {"code": "197361", "display": "amlodipine 10 MG", "status": "stopped", "dosage": ""})
    assert item["resource"]["status"] == "stopped" and "dosage" not in item["resource"]
    item = edit_item(draft, "medicationstatement-1",
                     {"code": "197361", "display": "amlodipine 10 MG", "status": "active", "dosage": "10 mg daily"})
    assert item["resource"]["dosage"] == [{"text": "10 mg daily"}]


def test_edit_transcript(draft):
    item = edit_item(draft, "documentreference-1", {"transcript": "Clinician: Corrected text."})
    data = item["resource"]["content"][0]["attachment"]["data"]
    assert base64.b64decode(data).decode() == "Clinician: Corrected text."
    assert validate(item["resource"]) == []


def test_add_condition_and_medication_are_clinician_entered(draft):
    cond = add_item(draft, dict(COND, resourceType="Condition", code="E11.9", display="Type 2 diabetes"),
                    "2026-11-27T10:05:00Z")
    med = add_item(draft, {"resourceType": "MedicationStatement", "code": "860975", "display": "metformin 500 MG",
                           "status": "active", "dosage": "500 mg twice daily"}, "2026-11-27T10:05:00Z")
    assert (cond["id"], med["id"]) == ("condition-2", "medicationstatement-2")
    for item in (cond, med):
        res = item["resource"]
        assert item["origin"] == "clinician" and item["selected"]
        assert res["meta"] == {"tag": [ENTERED_TAG]} and "security" not in res["meta"]
        assert res["subject"] == {"reference": "Patient/patient-1"}
        assert validate(res) == []


@pytest.mark.parametrize("item_id, fields, code", [
    ("condition-1", dict(COND, code="hypertension"), "BAD_CODE"),
    ("condition-1", dict(COND, display=""), "MISSING_FIELD"),
    ("condition-1", dict(COND, display="x" * 201), "FIELD_TOO_LONG"),
    ("condition-1", dict(COND, verificationStatus="maybe"), "BAD_FIELD"),
    ("condition-1", dict(COND, display="bad\x00text"), "BAD_FIELD"),
    ("medicationstatement-1", {"code": "abc", "display": "x", "status": "active"}, "BAD_CODE"),
    ("documentreference-1", {"transcript": "x" * 10_001}, "FIELD_TOO_LONG"),
    ("condition-99", COND, "ITEM_NOT_FOUND"),
    ("../x", COND, "BAD_ITEM_ID"),
])
def test_bad_edits_change_nothing(draft, item_id, fields, code):
    before = repr(draft)
    with pytest.raises(EditError) as exc:
        edit_item(draft, item_id, fields)
    assert exc.value.code == code
    assert repr(draft) == before


def test_add_rejects_other_resource_types(draft):
    with pytest.raises(EditError) as exc:
        add_item(draft, {"resourceType": "Observation"}, "2026-11-27T10:00:00Z")
    assert exc.value.code == "BAD_RESOURCE_TYPE"


def test_feedback_rules():
    assert parse_feedback({}, required=False) is None
    assert parse_feedback({"feedback": "  good draft  "}, required=False) == "good draft"
    with pytest.raises(EditError):
        parse_feedback({}, required=True)
    with pytest.raises(EditError):
        parse_feedback({"feedback": "x" * 2001}, required=False)
    draft = {}
    set_review(draft, "APPROVED", None, "sub-1", "2026-11-27T10:00:00Z")
    assert draft["review"] == {"decision": "APPROVED", "feedback": None, "reviewedBy": "sub-1",
                               "reviewedAt": "2026-11-27T10:00:00Z"}
