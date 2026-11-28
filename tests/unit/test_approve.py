"""Approve -> HealthLake: idempotency, resume after partial failure, write lock, validation."""

import boto3
import pytest
from moto import mock_aws

from conftest import cm_entity
from shared import drafts_table as dt
from shared.approve import ApproveError, approve
from shared.fhir_mapper import build_draft
from shared.healthlake import HealthLakeError

DRAFT_ID = "enc-1"
USER = {"sub": "clinician-sub", "iss": "https://cognito-idp.us-east-1.amazonaws.com/pool", "name": "Demo Clinician"}


def _draft():
    return build_draft(
        draft_id=DRAFT_ID, patient_id="patient-1", encounter_id=DRAFT_ID,
        turns=[{"index": 0, "role": "Clinician", "text": "Hypertension, amlodipine.", "confidence": 0.99}],
        nlp={"icd10cm": [cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"],
                                   codes=[("I10", "Essential (primary) hypertension", 0.9)])],
             "rxnorm": [cm_entity("amlodipine", "MEDICATION", codes=[("197361", "amlodipine 5 MG", 0.9)])]},
        now="2026-11-27T10:00:00Z",
    )


class FakeHealthLake:
    def __init__(self, fail_on=None, status=503):
        self.calls, self.fail_on, self.status, self.counter = [], fail_on, status, 0

    def _maybe_fail(self, kind):
        if self.fail_on == kind:
            self.fail_on = None  # fail once
            raise HealthLakeError(self.status, ["exception"])

    def put(self, resource):
        self._maybe_fail(resource["resourceType"])
        self.calls.append(("PUT", resource["resourceType"], resource))
        return f"{resource['resourceType']}/{resource['id']}"

    def create(self, resource):
        self._maybe_fail(resource["resourceType"])
        self.counter += 1
        self.calls.append(("POST", resource["resourceType"], resource))
        return f"{resource['resourceType']}/hl-{self.counter}"


@pytest.fixture
def env(lambda_env):
    with mock_aws():
        table = boto3.resource("dynamodb").create_table(
            TableName="test-drafts",
            KeySchema=[{"AttributeName": "draftId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "draftId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item={"draftId": DRAFT_ID, "status": "PENDING_REVIEW", "patientId": "patient-1",
                             "encounterId": DRAFT_ID, "createdAt": "2026-11-27T09:59:00Z"})
        draft = _draft()
        yield {"table": dt.DraftsTable(table), "raw": table, "draft": draft}


def run(env, ids, healthlake, clock=lambda: 1_000_000):
    return approve(draft_id=DRAFT_ID, requested_ids=ids, user=USER, table=env["table"],
                   read_draft=lambda _: env["draft"], healthlake=healthlake, clock=clock)


ALL = ["documentreference-1", "condition-1", "medicationstatement-1"]


def test_writes_encounter_selected_resources_then_provenance(env):
    hl = FakeHealthLake()
    result = run(env, ["condition-1", "documentreference-1"], hl)

    assert [(m, k) for m, k, _ in hl.calls] == [
        ("PUT", "Encounter"), ("POST", "DocumentReference"), ("POST", "Condition"), ("POST", "Provenance")]
    assert result["draft"]["status"] == "WRITTEN"
    assert [(w["proposedId"], w["resourceType"]) for w in result["written"]] == [
        ("encounter", "Encounter"), ("documentreference-1", "DocumentReference"),
        ("condition-1", "Condition"), ("provenance", "Provenance")]

    provenance = hl.calls[-1][2]
    assert provenance["target"] == [{"reference": "DocumentReference/hl-1"}, {"reference": "Condition/hl-2"}]
    roles = {a["type"]["coding"][0]["code"]: a["who"] for a in provenance["agent"]}
    assert roles["author"]["display"].startswith("AI-assisted pipeline")
    assert roles["verifier"] == {"display": "Demo Clinician",
                                 "identifier": {"system": USER["iss"], "value": USER["sub"]}}
    encounter = hl.calls[0][2]
    assert encounter["id"] == DRAFT_ID and encounter["subject"] == {"reference": "Patient/patient-1"}


def test_unselected_resources_are_never_written(env):
    hl = FakeHealthLake()
    run(env, ["condition-1"], hl)
    assert "MedicationStatement" not in [k for _, k, _ in hl.calls]


def test_repeat_approve_is_idempotent(env):
    hl = FakeHealthLake()
    first = run(env, ALL, hl)
    calls = len(hl.calls)
    second = run(env, list(reversed(ALL)), hl)
    assert len(hl.calls) == calls
    assert second["written"] == first["written"]


def test_different_selection_after_written_is_rejected(env):
    run(env, ALL, FakeHealthLake())
    with pytest.raises(ApproveError) as exc:
        run(env, ["condition-1"], FakeHealthLake())
    assert (exc.value.status, exc.value.code) == (409, "ALREADY_WRITTEN")


def test_partial_failure_resumes_without_duplicates(env):
    hl = FakeHealthLake(fail_on="Condition", status=503)
    with pytest.raises(ApproveError) as exc:
        run(env, ALL, hl)
    assert (exc.value.status, exc.value.code, exc.value.extra["writtenCount"]) == (502, "HEALTHLAKE_WRITE_FAILED", 2)
    item = env["table"].get(DRAFT_ID)
    assert item["status"] == "APPROVED" and "lockUntil" not in item
    assert set(item["healthLakeIds"]) == {"encounter", "documentreference-1"}

    result = run(env, ALL, hl)
    kinds = [k for _, k, _ in hl.calls]
    assert kinds.count("Encounter") == 1 and kinds.count("DocumentReference") == 1
    assert kinds.count("Condition") == 1 and kinds.count("Provenance") == 1
    assert result["draft"]["status"] == "WRITTEN"


def test_retry_with_a_different_selection_while_approved_is_rejected(env):
    with pytest.raises(ApproveError):
        run(env, ALL, FakeHealthLake(fail_on="Condition"))
    with pytest.raises(ApproveError) as exc:
        run(env, ["condition-1"], FakeHealthLake())
    assert exc.value.code == "APPROVAL_MISMATCH"


def test_concurrent_approve_is_blocked_by_the_write_lock(env):
    env["table"].start_approval(DRAFT_ID, sorted(ALL), "other-tab", now_epoch=1_000_000, lock_seconds=60)
    with pytest.raises(ApproveError) as exc:
        run(env, ALL, FakeHealthLake(), clock=lambda: 1_000_030)
    assert (exc.value.status, exc.value.code) == (409, "WRITE_IN_PROGRESS")
    # After the lock expires (e.g. the first Lambda died), a retry can take over.
    assert run(env, ALL, FakeHealthLake(), clock=lambda: 1_000_061)["draft"]["status"] == "WRITTEN"


def test_permanent_healthlake_rejection_marks_failed(env):
    with pytest.raises(ApproveError) as exc:
        run(env, ALL, FakeHealthLake(fail_on="Condition", status=400))
    assert exc.value.code == "HEALTHLAKE_REJECTED"
    item = env["table"].get(DRAFT_ID)
    assert (item["status"], item["errorCode"]) == ("FAILED", "HEALTHLAKE_REJECTED_400")


@pytest.mark.parametrize("ids, code", [
    ([], "BAD_SELECTION"), ("condition-1", "BAD_SELECTION"), ([1], "BAD_SELECTION"),
    (["condition-99"], "UNKNOWN_RESOURCE_IDS"),
])
def test_bad_selections(env, ids, code):
    hl = FakeHealthLake()
    with pytest.raises(ApproveError) as exc:
        run(env, ids, hl)
    assert exc.value.code == code and hl.calls == []
    assert env["table"].get(DRAFT_ID)["status"] == "PENDING_REVIEW"


def test_invalid_resource_blocks_every_write(env):
    env["draft"]["proposed"][1]["resource"]["subject"] = {"reference": "Group/x"}
    hl = FakeHealthLake()
    with pytest.raises(ApproveError) as exc:
        run(env, ALL, hl)
    assert (exc.value.status, exc.value.code, exc.value.extra["ids"]) == (422, "VALIDATION_FAILED", ["condition-1"])
    assert hl.calls == []


def test_rejected_or_failed_drafts_cannot_be_approved(env):
    env["raw"].update_item(Key={"draftId": DRAFT_ID}, UpdateExpression="SET #s = :s",
                           ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "REJECTED"})
    with pytest.raises(ApproveError) as exc:
        run(env, ALL, FakeHealthLake())
    assert (exc.value.status, exc.value.code) == (409, "NOT_PENDING_REVIEW")


def test_clinician_added_items_get_their_own_provenance(env):
    from shared.draft_edit import add_item

    add_item(env["draft"], {"resourceType": "Condition", "code": "E11.9", "display": "Type 2 diabetes",
                            "clinicalStatus": "active", "verificationStatus": "confirmed"}, "2026-11-27T10:05:00Z")
    hl = FakeHealthLake()
    result = run(env, ["condition-1", "condition-2"], hl)
    provenances = [r for m, k, r in hl.calls if k == "Provenance"]
    assert len(provenances) == 2
    ai, manual = provenances
    assert ai["target"] == [{"reference": "Condition/hl-1"}]
    assert {a["type"]["coding"][0]["code"] for a in ai["agent"]} == {"author", "verifier"}
    assert manual["target"] == [{"reference": "Condition/hl-2"}]
    assert [a["type"]["coding"][0]["code"] for a in manual["agent"]] == ["author"]
    assert manual["agent"][0]["who"]["identifier"]["value"] == USER["sub"]
    assert "security" not in manual["meta"]
    assert [w["proposedId"] for w in result["written"]][-2:] == ["provenance", "provenance-clinician"]


def test_optional_feedback_is_saved_once(env):
    saved = []
    approve(draft_id=DRAFT_ID, requested_ids=ALL, user=USER, table=env["table"], read_draft=lambda _: env["draft"],
            healthlake=FakeHealthLake(), clock=lambda: 1_000_000, feedback="Good draft.",
            save_review=lambda draft, text: saved.append(text) or draft.__setitem__("review", {"feedback": text}))
    assert saved == ["Good draft."]


def test_feedback_save_failure_releases_lock_and_allows_retry(env):
    def broken(draft, text):
        raise RuntimeError("s3 down")

    with pytest.raises(ApproveError) as exc:
        approve(draft_id=DRAFT_ID, requested_ids=ALL, user=USER, table=env["table"],
                read_draft=lambda _: env["draft"], healthlake=FakeHealthLake(), clock=lambda: 1_000_000,
                feedback="x", save_review=broken)
    assert exc.value.code == "FEEDBACK_NOT_SAVED"
    item = env["table"].get(DRAFT_ID)
    assert item["status"] == "APPROVED" and "lockUntil" not in item
    assert run(env, ALL, FakeHealthLake())["draft"]["status"] == "WRITTEN"


def test_encounter_uses_the_clinician_consultation_time(env):
    hl = FakeHealthLake()
    approve(draft_id=DRAFT_ID, requested_ids=ALL, user=USER, table=env["table"], read_draft=lambda _: env["draft"],
            healthlake=hl, clock=lambda: 1_000_000, encounter_start="2026-11-20T09:30:00Z")
    encounter = next(r for m, k, r in hl.calls if k == "Encounter")
    assert encounter["period"] == {"start": "2026-11-20T09:30:00Z"}
