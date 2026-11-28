"""API router against moto (S3, DynamoDB) with fake HealthLake and Transcribe clients."""

import json
from datetime import date

import boto3
import pytest
from moto import mock_aws

from conftest import cm_entity
from shared.healthlake import HealthLakeError

BUCKET = "test-data-bucket"
PATIENT = "patient-1"
USER = {"sub": "user-sub-1"}


class FakeHealthLake:
    def __init__(self):
        self.patients = {PATIENT: {"resourceType": "Patient", "id": PATIENT, "gender": "female",
                                   "birthDate": "1962-07-15",
                                   "name": [{"given": ["Test"], "family": "Patient"}]}}

    def search(self, resource_type, params):
        if resource_type == "Patient":
            return list(self.patients.values())
        assert params["clinical-status"] == "active"
        return [{"resourceType": "Condition", "code": {"text": "Essential hypertension"}},
                {"resourceType": "Condition", "code": {"coding": [{"display": "Gout"}]}}]

    def read(self, resource_type, resource_id):
        if resource_id not in self.patients:
            raise HealthLakeError(404, ["not-found"])
        return self.patients[resource_id]


class FakeTranscribe:
    status = "IN_PROGRESS"

    def get_medical_transcription_job(self, MedicalTranscriptionJobName):
        return {"MedicalTranscriptionJob": {"TranscriptionJobStatus": self.status}}


def event(route, *, body=None, draft_id=None, claims=True):
    e = {"routeKey": route, "requestContext": {"authorizer": {"jwt": {"claims": {"sub": USER["sub"]}}}}}
    if not claims:
        e["requestContext"] = {}
    if body is not None:
        e["body"] = body if isinstance(body, str) else json.dumps(body)
    if draft_id is not None:
        e["pathParameters"] = {"id": draft_id}
    return e


def call(api, *args, **kwargs):
    response = api.handler(event(*args, **kwargs), None)
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def api(lambda_env, monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", BUCKET)
    monkeypatch.setenv("HEALTHLAKE_DATASTORE_ID", "ds-1")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key="samples/consult-01.wav", Body=b"RIFF")
        s3.put_object(Bucket=BUCKET, Key="samples/readme.txt", Body=b"x")
        table = boto3.resource("dynamodb").create_table(
            TableName="test-drafts",
            KeySchema=[{"AttributeName": "draftId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "draftId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        import api as api_module
        from shared.drafts_table import DraftsTable

        drafts, transcribe = DraftsTable(table), FakeTranscribe()
        monkeypatch.setattr(api_module, "_s3", lambda: s3)
        monkeypatch.setattr(api_module, "_table", lambda: drafts)
        monkeypatch.setattr(api_module, "_healthlake", lambda: FakeHealthLake())
        monkeypatch.setattr(api_module, "_transcribe", lambda: transcribe)
        monkeypatch.setattr(api_module, "_today", lambda: date(2026, 11, 27))
        api_module.test_ctx = {"s3": s3, "table": table, "transcribe": transcribe}
        yield api_module


def test_missing_claims_is_401_and_unknown_route_is_404(api):
    assert call(api, "GET /patients", claims=False)[0] == 401
    assert call(api, "DELETE /patients")[0] == 404


def test_patients_have_age_sex_and_active_conditions(api):
    status, body = call(api, "GET /patients")
    assert status == 200
    assert body["patients"] == [{
        "id": PATIENT, "name": "Test Patient", "gender": "female", "birthDate": "1962-07-15", "age": 64,
        "activeConditions": ["Essential hypertension", "Gout"],
    }]


def test_samples_lists_only_audio_files(api):
    status, body = call(api, "GET /samples")
    assert status == 200
    assert [{k: v for k, v in x.items() if k != "url"} for x in body["samples"]] == [
        {"key": "samples/consult-01.wav", "name": "consult-01.wav", "sizeBytes": 4}]


def test_process_creates_draft_and_copies_sample_into_audio(api):
    status, body = call(api, "POST /process", body={"patientId": PATIENT, "sampleKey": "samples/consult-01.wav"})
    assert status == 202 and body["status"] == "UPLOADED"
    draft_id = body["draftId"]
    item = api.test_ctx["table"].get_item(Key={"draftId": draft_id})["Item"]
    assert (item["status"], item["patientId"], item["createdBy"]) == ("UPLOADED", PATIENT, USER["sub"])
    copied = api.test_ctx["s3"].get_object(Bucket=BUCKET, Key=f"audio/{PATIENT}/{draft_id}/consult.wav")
    assert copied["Body"].read() == b"RIFF"


@pytest.mark.parametrize("body, code", [
    ({"patientId": "../x", "sampleKey": "samples/consult-01.wav"}, "BAD_PATIENT_ID"),
    ({"patientId": PATIENT, "sampleKey": "drafts/x.json"}, "BAD_SAMPLE_KEY"),
    ({"patientId": PATIENT, "sampleKey": "samples/../audio/x.wav"}, "BAD_SAMPLE_KEY"),
    ({"patientId": "unknown-patient", "sampleKey": "samples/consult-01.wav"}, "UNKNOWN_PATIENT"),
    ({"patientId": PATIENT, "sampleKey": "samples/missing.wav"}, "BAD_SAMPLE_KEY"),
    ("not json", "BAD_REQUEST"),
    ("[1, 2]", "BAD_REQUEST"),
])
def test_process_rejects_bad_input(api, body, code):
    status, response = call(api, "POST /process", body=body)
    assert (status, response["error"]) == (400, code)


def _pending_draft(api, draft_id="d-1"):
    api.test_ctx["table"].put_item(Item={"draftId": draft_id, "status": "PENDING_REVIEW", "patientId": PATIENT,
                                         "encounterId": draft_id, "createdAt": "2026-11-27T10:00:00Z",
                                         "proposedCount": 2, "secretInternal": "x"})
    draft = {"turns": [{"index": 0, "role": "Clinician", "text": "Hi."}], "proposed": [{"id": "condition-1"}],
             "excluded": [], "confidenceThreshold": 0.8, "codeScoreFloor": 0.3,
             "patientId": PATIENT, "encounterId": draft_id}
    nlp = {"icd10cm": [cm_entity("gout", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], begin=5,
                                 codes=[("M10.9", "Gout, unspecified", 0.7)])],
           "rxnorm": [],
           "entities": [{"Text": "wrist", "Category": "ANATOMY", "Type": "SYSTEM_ORGAN_SITE", "Score": 0.9,
                         "BeginOffset": 1, "Traits": []},
                        {"Text": "gout", "Category": "MEDICAL_CONDITION", "Score": 0.9, "BeginOffset": 5}]}
    for key, body in ((f"drafts/{draft_id}.json", draft), (f"nlp/{draft_id}.json", nlp)):
        api.test_ctx["s3"].put_object(Bucket=BUCKET, Key=key, Body=json.dumps(body))


def test_get_draft_returns_review_payload_with_entity_rows(api):
    _pending_draft(api)
    status, body = call(api, "GET /drafts/{id}", draft_id="d-1")
    assert status == 200
    assert body["draft"]["status"] == "PENDING_REVIEW"
    assert "secretInternal" not in body["draft"]
    assert body["proposed"] == [{"id": "condition-1"}]
    assert body["thresholds"] == {"confidence": 0.8, "codeScoreFloor": 0.3}
    assert [(r["text"], r["category"], r["code"]) for r in body["entities"]] == [
        ("wrist", "ANATOMY", None), ("gout", "MEDICAL_CONDITION", "M10.9")]


def test_get_draft_while_processing_has_status_only(api):
    api.test_ctx["table"].put_item(Item={"draftId": "d-2", "status": "ANALYZING"})
    status, body = call(api, "GET /drafts/{id}", draft_id="d-2")
    assert status == 200 and set(body) == {"draft"}


def test_failed_transcription_job_is_detected_and_marked_failed(api):
    api.test_ctx["table"].put_item(Item={"draftId": "d-3", "status": "TRANSCRIBING", "jobName": "job-d-3"})
    assert call(api, "GET /drafts/{id}", draft_id="d-3")[1]["draft"]["status"] == "TRANSCRIBING"
    api.test_ctx["transcribe"].status = "FAILED"
    draft = call(api, "GET /drafts/{id}", draft_id="d-3")[1]["draft"]
    assert (draft["status"], draft["errorCode"]) == ("FAILED", "TRANSCRIBE_JOB_FAILED")


def test_get_draft_unknown_and_bad_ids(api):
    assert call(api, "GET /drafts/{id}", draft_id="nope")[0] == 404
    assert call(api, "GET /drafts/{id}", draft_id="../etc")[0] == 400


def test_list_drafts_newest_first(api):
    for i, created in enumerate(["2026-11-27T09:00:00Z", "2026-11-27T11:00:00Z"]):
        api.test_ctx["table"].put_item(Item={"draftId": f"d-{i}", "status": "UPLOADED", "createdAt": created})
    status, body = call(api, "GET /drafts")
    assert status == 200 and [d["draftId"] for d in body["drafts"]] == ["d-1", "d-0"]


FEEDBACK = {"feedback": "Synthetic test: transcript too noisy to use."}


def test_reject_needs_feedback_and_only_from_pending_review(api):
    _pending_draft(api)
    for body in ({}, {"feedback": "   "}, {"feedback": "ok"}):
        status, response = call(api, "POST /drafts/{id}/reject", draft_id="d-1", body=body)
        assert (status, response["error"]) == (400, "FEEDBACK_REQUIRED")
    status, body = call(api, "POST /drafts/{id}/reject", draft_id="d-1", body=FEEDBACK)
    assert status == 200 and body["draft"]["status"] == "REJECTED" and body["draft"]["hasFeedback"] is True
    item = api.test_ctx["table"].get_item(Key={"draftId": "d-1"})["Item"]
    assert item["reviewedBy"] == USER["sub"]
    assert FEEDBACK["feedback"] not in json.dumps(item, default=str)  # never in DynamoDB
    stored = json.loads(api.test_ctx["s3"].get_object(Bucket=BUCKET, Key="drafts/d-1.json")["Body"].read())
    assert stored["review"]["decision"] == "REJECTED" and stored["review"]["feedback"] == FEEDBACK["feedback"]
    assert call(api, "POST /drafts/{id}/reject", draft_id="d-1", body=FEEDBACK)[1]["error"] == "NOT_PENDING_REVIEW"
    assert call(api, "POST /drafts/{id}/reject", draft_id="missing", body=FEEDBACK)[0] == 404


def test_feedback_is_never_logged(api, capsys):
    _pending_draft(api)
    call(api, "POST /drafts/{id}/reject", draft_id="d-1", body=FEEDBACK)
    assert "noisy" not in capsys.readouterr().out


def test_healthlake_failure_is_502_without_details(api, monkeypatch):
    class Broken(FakeHealthLake):
        def search(self, *a):
            raise HealthLakeError(500, ["exception"])

    monkeypatch.setattr(api, "_healthlake", lambda: Broken())
    status, body = call(api, "GET /patients")
    assert (status, body["error"]) == (502, "HEALTHLAKE_ERROR")


def test_approve_route_validates_selection_and_returns_written(api, monkeypatch):
    _pending_draft(api)
    status, body = call(api, "POST /drafts/{id}/approve", draft_id="d-1", body={"resourceIds": []})
    assert (status, body["error"]) == (400, "BAD_SELECTION")

    import shared.approve as approve_module

    def fake_approve(**kwargs):
        assert kwargs["user"] == {"sub": USER["sub"], "iss": None, "name": None}
        return {"draft": {"draftId": "d-1", "status": "WRITTEN", "lockUntil": 1},
                "written": [{"proposedId": "encounter", "resourceType": "Encounter", "id": "d-1"}]}

    monkeypatch.setattr(api, "approve", fake_approve)
    status, body = call(api, "POST /drafts/{id}/approve", draft_id="d-1", body={"resourceIds": ["condition-1"]})
    assert status == 200 and body["written"][0]["resourceType"] == "Encounter"
    assert "lockUntil" not in body["draft"]
    assert approve_module.approve is not fake_approve


# --- audio links and uploads -------------------------------------------------------------------

import base64  # noqa: E402
import io  # noqa: E402
import wave  # noqa: E402


def _wav(rate=16000, seconds=0.1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def upload_event(data: bytes, content_type="audio/wav", patient=PATIENT, b64=True):
    e = event("POST /process/upload")
    e["queryStringParameters"] = {"patientId": patient}
    e["headers"] = {"content-type": content_type}
    e["isBase64Encoded"] = b64
    e["body"] = base64.b64encode(data).decode() if b64 else data.decode("latin-1")
    return e


def upload_call(api, *args, **kwargs):
    response = api.handler(upload_event(*args, **kwargs), None)
    return response["statusCode"], json.loads(response["body"])


def test_samples_and_drafts_carry_presigned_regional_audio_links(api):
    [sample] = call(api, "GET /samples")[1]["samples"]
    assert sample["url"].startswith(f"https://{BUCKET}.s3.us-east-1.amazonaws.com/samples/consult-01.wav?")
    assert "X-Amz-Expires=3600" in sample["url"]

    api.test_ctx["table"].put_item(Item={"draftId": "d-9", "status": "ANALYZING", "audioKey": "audio/p/d-9/x.wav"})
    body = call(api, "GET /drafts/{id}", draft_id="d-9")[1]
    assert body["audioUrl"].startswith(f"https://{BUCKET}.s3.us-east-1.amazonaws.com/audio/p/d-9/x.wav?")


def test_upload_stores_audio_under_audio_prefix_and_creates_draft(api):
    data = _wav()
    status, body = upload_call(api, data)
    assert status == 202
    draft_id = body["draftId"]
    item = api.test_ctx["table"].get_item(Key={"draftId": draft_id})["Item"]
    assert (item["status"], item["source"], item["audioKey"]) == ("UPLOADED", "upload", f"audio/{PATIENT}/{draft_id}/upload.wav")
    stored = api.test_ctx["s3"].get_object(Bucket=BUCKET, Key=item["audioKey"])
    assert stored["Body"].read() == data and stored["ContentType"] == "audio/wav"


@pytest.mark.parametrize("kwargs, code", [
    ({"data": b"RIFF....WAVE", "content_type": "text/plain"}, "UNSUPPORTED_TYPE"),
    ({"data": b"not audio at all", "content_type": "audio/wav"}, "NOT_AUDIO"),
    ({"data": b"not audio at all", "content_type": "audio/mpeg"}, "NOT_AUDIO"),
    ({"data": b"RIFF....WAVE", "b64": False}, "EMPTY_FILE"),
    ({"data": b"", "content_type": "audio/wav"}, "EMPTY_FILE"),
    ({"data": b"\x00" * (4 * 1024 * 1024 + 1)}, "FILE_TOO_LARGE"),
    ({"data": None, "patient": "unknown-patient"}, "UNKNOWN_PATIENT"),
    ({"data": None, "patient": "../x"}, "BAD_PATIENT_ID"),
    ({"data": "8k"}, "LOW_SAMPLE_RATE"),
])
def test_upload_rejections_write_nothing(api, kwargs, code):
    data = kwargs.pop("data")
    data = _wav(8000) if data == "8k" else (_wav() if data is None else data)
    status, body = upload_call(api, data, **kwargs)
    assert (status, body["error"]) == (400, code)
    assert api.test_ctx["table"].scan()["Count"] == 0
    assert "Contents" not in api.test_ctx["s3"].list_objects_v2(Bucket=BUCKET, Prefix="audio/")


@pytest.mark.parametrize("content_type, header", [
    ("audio/mpeg", b"ID3\x04" + b"\x00" * 20), ("audio/flac", b"fLaC" + b"\x00" * 20),
    ("audio/x-m4a", b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 20),
])
def test_upload_accepts_other_formats(api, content_type, header):
    status, body = upload_call(api, header, content_type=content_type)
    assert status == 202


def test_entity_rows_carry_trait_scores(api):
    _pending_draft(api)
    rows = call(api, "GET /drafts/{id}", draft_id="d-1")[1]["entities"]
    gout = next(r for r in rows if r["text"] == "gout")
    assert gout["traits"] == [{"name": "DIAGNOSIS", "score": 0.9}]
    assert gout["score"] == 0.95 and gout["codeScore"] == 0.7



def test_add_and_edit_items_through_the_api(api):
    _pending_draft(api)
    status, body = call(api, "POST /drafts/{id}/items", draft_id="d-1", body={
        "resourceType": "Condition", "code": "m10.9", "display": "Gout, unspecified",
        "clinicalStatus": "active", "verificationStatus": "confirmed"})
    assert status == 201
    item = body["item"]
    assert (item["id"], item["code"], item["origin"], item["selected"]) == ("condition-2", "M10.9", "clinician", True)

    status, body = call(api, "PUT /drafts/{id}/items/{itemId}", draft_id="d-1", body={
        "code": "M10.9", "display": "Gout", "clinicalStatus": "remission", "verificationStatus": "confirmed"})
    assert (status, body["error"]) == (400, "BAD_ITEM_ID")  # no itemId path parameter

    e = event("PUT /drafts/{id}/items/{itemId}", body={"code": "Z99", "display": "x", "clinicalStatus": "active",
                                                        "verificationStatus": "nope"})
    e["pathParameters"] = {"id": "d-1", "itemId": "condition-2"}
    response = api.handler(e, None)
    assert (response["statusCode"], json.loads(response["body"])["error"]) == (400, "BAD_FIELD")

    stored = json.loads(api.test_ctx["s3"].get_object(Bucket=BUCKET, Key="drafts/d-1.json")["Body"].read())
    assert [p["id"] for p in stored["proposed"]] == ["condition-1", "condition-2"]


def test_items_cannot_change_after_review(api):
    _pending_draft(api)
    call(api, "POST /drafts/{id}/reject", draft_id="d-1", body=FEEDBACK)
    status, body = call(api, "POST /drafts/{id}/items", draft_id="d-1", body={"resourceType": "Condition"})
    assert (status, body["error"]) == (409, "NOT_PENDING_REVIEW")


def test_patient_summary_route(api, monkeypatch):
    class ChartHealthLake(FakeHealthLake):
        def search(self, resource_type, params):
            assert params == {"subject": f"Patient/{PATIENT}", "_count": "100"}
            if resource_type == "Encounter":
                return [{"status": "finished", "type": [{"text": "Visit"}], "period": {"start": "2026-01-01"}}]
            return []

    monkeypatch.setattr(api, "_healthlake", lambda: ChartHealthLake())
    status, body = call(api, "GET /patients/{id}/summary", draft_id=PATIENT)
    assert status == 200
    assert body["patient"]["name"] == "Test Patient" and body["encounters"][0]["type"] == "Visit"
    assert set(body) == {"patient", "problems", "medications", "vitals", "labs", "encounters", "notes"}
    assert call(api, "GET /patients/{id}/summary", draft_id="unknown-patient")[0] == 404
    assert call(api, "GET /patients/{id}/summary", draft_id="../x")[0] == 400


def test_fhir_view_returns_raw_healthlake_response_with_request_details(api, monkeypatch):
    calls = []

    class ViewHealthLake(FakeHealthLake):
        base_url = "https://healthlake.us-east-1.amazonaws.com/datastore/ds-1/r4/"

        def request(self, method, path, params=None):
            calls.append((method, path, params))
            if path == "Observation":
                raise HealthLakeError(500, ["exception"])
            return 200, {"resourceType": "Bundle", "entry": []}, {"X-Amzn-RequestId": "req-123"}

    monkeypatch.setattr(api, "_healthlake", lambda: ViewHealthLake())
    e = event("GET /patients/{id}/fhir", draft_id=PATIENT)
    e["queryStringParameters"] = {"type": "Condition"}
    body = json.loads(api.handler(e, None)["body"])
    assert body["request"]["url"] == (
        f"https://healthlake.us-east-1.amazonaws.com/datastore/ds-1/r4/Condition?subject=Patient%2F{PATIENT}&_count=100")
    assert (body["status"], body["requestId"], body["response"]) == (200, "req-123", {"resourceType": "Bundle", "entry": []})
    assert calls[-1] == ("GET", "Condition", {"subject": f"Patient/{PATIENT}", "_count": "100"})

    e["queryStringParameters"] = {}
    body = json.loads(api.handler(e, None)["body"])
    assert calls[-1] == ("GET", f"Patient/{PATIENT}", None) and body["request"]["url"].endswith(f"/Patient/{PATIENT}")

    e["queryStringParameters"] = {"type": "Observation"}
    body = json.loads(api.handler(e, None)["body"])
    assert body["status"] == 500 and body["response"] == {"issue": [{"code": "exception"}]}

    e["queryStringParameters"] = {"type": "Account"}
    assert api.handler(e, None)["statusCode"] == 400


def test_update_encounter_route_validates_input(api):
    e = event("PUT /patients/{id}/encounters/{encounterId}", body={"start": "not a date"})
    e["pathParameters"] = {"id": PATIENT, "encounterId": "enc-1"}
    response = api.handler(e, None)
    assert (response["statusCode"], json.loads(response["body"])["error"]) == (400, "BAD_DATETIME")
    e["pathParameters"] = {"id": PATIENT, "encounterId": "../x"}
    assert api.handler(e, None)["statusCode"] == 400


# --- HealthScribe options and notes ------------------------------------------------------------

def test_process_stores_engine_options_and_validates_them(api):
    status, body = call(api, "POST /process", body={"patientId": PATIENT, "sampleKey": "samples/consult-01.wav",
                                                     "engine": "both", "noteTemplate": "PHYSICAL_SOAP",
                                                     "pronouns": "THEY_THEM"})
    assert status == 202
    item = api.test_ctx["table"].get_item(Key={"draftId": body["draftId"]})["Item"]
    assert (item["engine"], item["noteTemplate"], item["pronouns"]) == ("both", "PHYSICAL_SOAP", "THEY_THEM")
    for bad, code in (({"engine": "gpt"}, "BAD_ENGINE"), ({"noteTemplate": "X"}, "BAD_TEMPLATE"),
                      ({"pronouns": "it"}, "BAD_PRONOUNS")):
        status, body = call(api, "POST /process", body={"patientId": PATIENT, "sampleKey": "samples/consult-01.wav", **bad})
        assert (status, body["error"]) == (400, code)


def _note(template):
    return {"template": template, "templateTitle": "SOAP (physical health)",
            "sections": [{"name": "PLAN", "title": "Plan", "items": [{"text": "Start amlodipine.", "evidence": []}]}]}


def test_use_note_rebuilds_the_note_item_and_get_draft_lists_notes(api):
    _pending_draft(api)
    api.test_ctx["s3"].put_object(Bucket=BUCKET, Key="notes/d-1/PHYSICAL_SOAP.json", Body=json.dumps(_note("PHYSICAL_SOAP")))
    e = event("POST /drafts/{id}/notes/{template}/use")
    e["pathParameters"] = {"id": "d-1", "template": "PHYSICAL_SOAP"}
    response = api.handler(e, None)
    item = json.loads(response["body"])["item"]
    assert response["statusCode"] == 200
    assert (item["id"], item["code"], item["noteTemplate"], item["selected"]) == ("documentreference-1", "11506-3",
                                                                                 "PHYSICAL_SOAP", False)
    body = call(api, "GET /drafts/{id}", draft_id="d-1")[1]
    assert set(body["notes"]) == {"PHYSICAL_SOAP"} and body["noteTemplate"] == "PHYSICAL_SOAP"

    e["pathParameters"] = {"id": "d-1", "template": "HISTORY_AND_PHYSICAL"}
    assert api.handler(e, None)["statusCode"] == 404


def test_generate_note_requires_pending_review_and_blocks_duplicates(api, monkeypatch):
    monkeypatch.setenv("JOB_PREFIX", "voice-to-fhir-test")
    monkeypatch.setenv("SCRIBE_ROLE_ARN", "arn:role")
    monkeypatch.setenv("WATCHER_FUNCTION", "watcher")
    started, invoked = [], []

    class Transcribe:
        def start_medical_scribe_job(self, **kw):
            started.append(kw)

    class Lambda:
        def invoke(self, **kw):
            invoked.append(json.loads(kw["Payload"]))

    monkeypatch.setattr(api, "_transcribe", lambda: Transcribe())
    monkeypatch.setattr(api, "_lambda", lambda: Lambda())
    _pending_draft(api)
    api.test_ctx["table"].update_item(Key={"draftId": "d-1"}, UpdateExpression="SET audioKey = :k",
                                      ExpressionAttributeValues={":k": f"audio/{PATIENT}/d-1/consult.wav"})
    status, body = call(api, "POST /drafts/{id}/notes", draft_id="d-1", body={"template": "DAP"})
    assert status == 202 and invoked[0]["mode"] == "note" and invoked[0]["template"] == "DAP"
    assert started[0]["Settings"]["ClinicalNoteGenerationSettings"] == {"NoteTemplate": "DAP"}
    assert call(api, "POST /drafts/{id}/notes", draft_id="d-1", body={"template": "DAP"})[1]["error"] == "NOTE_IN_PROGRESS"
    assert call(api, "POST /drafts/{id}/notes", draft_id="d-1", body={"template": "NOPE"})[1]["error"] == "BAD_TEMPLATE"
    draft = call(api, "GET /drafts/{id}", draft_id="d-1")[1]["draft"]
    assert draft["pendingNotes"] == ["DAP"]
