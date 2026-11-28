"""start_transcription / analyze_transcript against moto (S3, DynamoDB) and fake AI clients."""

import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from conftest import cm_entity, transcribe_output

BUCKET = "test-data-bucket"
PATIENT = "patient-1"
DRAFT = "11111111-2222-3333-4444-555555555555"
AUDIO_KEY = f"audio/{PATIENT}/{DRAFT}/consult.wav"


def wav_bytes(channels: int, rate: int = 16000) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * channels * 160)
    return buf.getvalue()


def s3_event(key: str) -> dict:
    return {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": key}}}]}


class FakeTranscribe:
    def __init__(self, error_code=None):
        self.calls, self.error_code = [], error_code

    def start_medical_transcription_job(self, **kwargs):
        self.calls.append(kwargs)
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "x"}}, "StartMedicalTranscriptionJob")
        if len(self.calls) > 1:
            raise ClientError({"Error": {"Code": "ConflictException", "Message": "x"}}, "StartMedicalTranscriptionJob")


class FakeComprehendMedical:
    def detect_entities_v2(self, Text):
        return {"Entities": [], "ModelVersion": "test"}

    def infer_icd10_cm(self, Text):
        entities = []
        if "chest pain" in Text:
            entities.append(cm_entity("chest pain", "MEDICAL_CONDITION", traits=["SYMPTOM", "NEGATION"],
                                      begin=Text.index("chest pain"), codes=[("R07.9", "Chest pain", 0.9)]))
        if "hypertension" in Text:
            entities.append(cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"],
                                      begin=Text.index("hypertension"),
                                      codes=[("I10", "Essential (primary) hypertension", 0.95)]))
        return {"Entities": entities, "ModelVersion": "test"}

    def infer_rx_norm(self, Text):
        return {"Entities": [], "ModelVersion": "test"}


@pytest.fixture
def aws(lambda_env, monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        ddb = boto3.resource("dynamodb")
        table = ddb.create_table(
            TableName="test-drafts",
            KeySchema=[{"AttributeName": "draftId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "draftId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        import analyze_transcript
        import start_transcription
        from shared.drafts_table import DraftsTable

        drafts = DraftsTable(table)
        for module in (start_transcription, analyze_transcript):
            monkeypatch.setattr(module, "_table", lambda: drafts)
        monkeypatch.setattr(analyze_transcript, "_s3", lambda: s3)
        monkeypatch.setattr(start_transcription, "_s3", lambda: s3)
        s3.put_object(Bucket=BUCKET, Key=AUDIO_KEY, Body=wav_bytes(channels=1))
        monkeypatch.setattr(analyze_transcript, "_comprehend_medical", lambda: FakeComprehendMedical())
        yield {"s3": s3, "table": table, "start": start_transcription, "analyze": analyze_transcript}


def status(aws):
    return aws["table"].get_item(Key={"draftId": DRAFT})["Item"]


# --- start_transcription -------------------------------------------------------------------

def test_start_creates_record_and_starts_job_with_expected_parameters(aws, monkeypatch):
    fake = FakeTranscribe()
    monkeypatch.setattr(aws["start"], "_transcribe", lambda: fake)

    assert aws["start"].handler(s3_event(AUDIO_KEY), None) == ["started"]

    item = status(aws)
    assert item["status"] == "TRANSCRIBING"
    assert (item["patientId"], item["encounterId"], item["audioKey"]) == (PATIENT, DRAFT, AUDIO_KEY)
    [call] = fake.calls
    assert call == {
        "MedicalTranscriptionJobName": f"voice-to-fhir-test-{DRAFT}",
        "LanguageCode": "en-US",
        "Media": {"MediaFileUri": f"s3://{BUCKET}/{AUDIO_KEY}"},
        "MediaFormat": "wav",
        "OutputBucketName": BUCKET,
        "OutputKey": f"transcripts/{DRAFT}.json",
        "Specialty": "PRIMARYCARE",
        "Type": "CONVERSATION",
        "Settings": {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 2},
    }


def test_duplicate_s3_event_does_not_start_a_second_job(aws, monkeypatch):
    fake = FakeTranscribe()
    monkeypatch.setattr(aws["start"], "_transcribe", lambda: fake)
    aws["start"].handler(s3_event(AUDIO_KEY), None)
    # Duplicate delivery while still TRANSCRIBING: start is retried, Transcribe says Conflict.
    assert aws["start"].handler(s3_event(AUDIO_KEY), None) == ["already_started"]
    assert status(aws)["status"] == "TRANSCRIBING"

    # Once the draft has moved on, a late duplicate is ignored without calling Transcribe.
    aws["table"].update_item(Key={"draftId": DRAFT}, UpdateExpression="SET #s = :s",
                             ExpressionAttributeNames={"#s": "status"},
                             ExpressionAttributeValues={":s": "PENDING_REVIEW"})
    assert aws["start"].handler(s3_event(AUDIO_KEY), None) == ["duplicate"]
    assert len(fake.calls) == 2


def test_non_retryable_transcribe_error_marks_failed_with_code(aws, monkeypatch):
    monkeypatch.setattr(aws["start"], "_transcribe", lambda: FakeTranscribe("BadRequestException"))
    assert aws["start"].handler(s3_event(AUDIO_KEY), None) == ["failed"]
    item = status(aws)
    assert (item["status"], item["errorCode"]) == ("FAILED", "TRANSCRIBE_BadRequestException")


def test_retryable_transcribe_error_raises_for_lambda_retry(aws, monkeypatch):
    monkeypatch.setattr(aws["start"], "_transcribe", lambda: FakeTranscribe("LimitExceededException"))
    with pytest.raises(ClientError):
        aws["start"].handler(s3_event(AUDIO_KEY), None)
    assert status(aws)["status"] == "TRANSCRIBING"


@pytest.mark.parametrize("key", [
    "audio/patient-1/consult.wav",              # missing encounter folder
    f"audio/patient-1/{DRAFT}/consult.txt",     # not audio
    f"audio/../{DRAFT}/x.wav",                  # path tricks
    "samples/consult.wav",
])
def test_unexpected_keys_are_ignored(aws, monkeypatch, key):
    fake = FakeTranscribe()
    monkeypatch.setattr(aws["start"], "_transcribe", lambda: fake)
    assert aws["start"].handler(s3_event(key), None) == ["ignored"]
    assert fake.calls == []


# --- analyze_transcript --------------------------------------------------------------------

def _transcribing(aws):
    aws["table"].put_item(Item={"draftId": DRAFT, "status": "TRANSCRIBING", "patientId": PATIENT,
                                "engines": ["transcribe"], "pendingEngines": {"transcribe"},
                                "encounterId": DRAFT})
    doc = transcribe_output([("spk_0", "Any chest pain?"), ("spk_1", "No chest pain. I have hypertension.")])
    aws["s3"].put_object(Bucket=BUCKET, Key=f"transcripts/{DRAFT}.json", Body=json.dumps(doc))


def test_analyze_writes_nlp_and_draft_then_pending_review(aws):
    _transcribing(aws)
    assert aws["analyze"].handler(s3_event(f"transcripts/{DRAFT}.json"), None) == ["ready"]

    item = status(aws)
    assert item["status"] == "PENDING_REVIEW"
    assert (item["turnCount"], item["proposedCount"], item["excludedCount"]) == (2, 2, 1)

    draft = json.loads(aws["s3"].get_object(Bucket=BUCKET, Key=f"drafts/{DRAFT}.json")["Body"].read())
    assert [p["resourceType"] for p in draft["proposed"]] == ["DocumentReference", "Condition"]
    assert draft["excluded"][0]["reason"] == "NEGATION"
    nlp = json.loads(aws["s3"].get_object(Bucket=BUCKET, Key=f"nlp/{DRAFT}.json")["Body"].read())
    assert nlp["draftId"] == DRAFT and len(nlp["icd10cm"]) == 2


def test_analyze_duplicate_event_after_review_is_skipped(aws):
    _transcribing(aws)
    aws["analyze"].handler(s3_event(f"transcripts/{DRAFT}.json"), None)
    assert aws["analyze"].handler(s3_event(f"transcripts/{DRAFT}.json"), None) == ["skipped"]
    assert status(aws)["status"] == "PENDING_REVIEW"


def test_analyze_ignores_transcribe_temp_file_and_other_keys(aws):
    for key in ["transcripts/.write_access_check_file.temp", ".write_access_check_file.temp",
                "transcripts/sub/x.json"]:
        assert aws["analyze"].handler(s3_event(key), None) == ["ignored"]


def test_analyze_empty_transcript_fails_with_code_only(aws, capsys):
    aws["table"].put_item(Item={"draftId": DRAFT, "status": "TRANSCRIBING", "patientId": PATIENT,
                                "engines": ["transcribe"], "pendingEngines": {"transcribe"},
                                "encounterId": DRAFT})
    aws["s3"].put_object(Bucket=BUCKET, Key=f"transcripts/{DRAFT}.json", Body=json.dumps({"results": {"items": []}}))
    assert aws["analyze"].handler(s3_event(f"transcripts/{DRAFT}.json"), None) == ["failed"]
    item = status(aws)
    assert (item["status"], item["errorCode"]) == ("FAILED", "ANALYZE_EMPTY_TRANSCRIPT")


def test_logs_never_contain_transcript_or_entity_text(aws, capsys):
    _transcribing(aws)
    aws["analyze"].handler(s3_event(f"transcripts/{DRAFT}.json"), None)
    out = capsys.readouterr().out.lower()
    for phrase in ("chest pain", "hypertension", "essential"):
        assert phrase not in out
