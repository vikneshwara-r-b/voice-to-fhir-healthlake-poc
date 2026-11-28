"""AWS HealthScribe path: parsing, job settings, engine choice, watcher and assembly (synthetic data)."""

import json

import boto3
import pytest
from moto import mock_aws

from conftest import cm_entity, transcribe_output
from shared import scribe
from shared.engines import parse_s3_uri
from shared.transcript_parser import parse_turns, role_method

BUCKET = "test-data-bucket"
PATIENT = "patient-1"
DRAFT = "11111111-2222-3333-4444-555555555555"
AUDIO_KEY = f"audio/{PATIENT}/{DRAFT}/consult.wav"


def scribe_transcript():
    segs = [("s1", "CLINICIAN_0", "SUBJECTIVE", "Any chest pain?"),
            ("s2", "PATIENT_0", "SUBJECTIVE", "No chest pain."),
            ("s3", "PATIENT_0", "SUBJECTIVE", "I have hypertension."),
            ("s4", "CLINICIAN_0", "ASSESSMENT_AND_PLAN", "Start amlodipine.")]
    return {"Conversation": {
        "ClinicalInsights": [{"InsightType": "ClinicalEntity", "Category": "MEDICAL_CONDITION", "Type": "DX_NAME",
                              "Spans": [{"Content": "hypertension", "SegmentId": "s3"}]}],
        "TranscriptSegments": [{"SegmentId": i, "Content": t, "BeginAudioTime": n, "EndAudioTime": n + 1,
                                "ParticipantDetails": {"ParticipantRole": r}, "SectionDetails": {"SectionName": sec}}
                               for n, (i, r, sec, t) in enumerate(segs)]}}


def scribe_note():
    return {"ClinicalDocumentation": {"Sections": [
        {"SectionName": "CHIEF_COMPLAINT", "Summary": [{"SummarizedSegment": "Blood pressure follow-up.",
                                                         "EvidenceLinks": [{"SegmentId": "s3"}]}]},
        {"SectionName": "PLAN", "Summary": [{"SummarizedSegment": "Start amlodipine.",
                                              "EvidenceLinks": [{"SegmentId": "s4"}, {"SegmentId": "missing"}]}]},
        {"SectionName": "REVIEW_OF_SYSTEMS", "Summary": []}]}}


# --- parsing -----------------------------------------------------------------------------------

def test_parse_transcript_uses_healthscribe_roles_and_sections():
    turns, segments, insights = scribe.parse_transcript(scribe_transcript())
    assert [(t["role"], t["text"]) for t in turns] == [
        ("Clinician", "Any chest pain?"), ("Patient", "No chest pain. I have hypertension."),
        ("Clinician", "Start amlodipine.")]
    assert [s["section"] for s in turns[1]["segments"]] == ["SUBJECTIVE", "SUBJECTIVE"]
    assert segments["s4"]["role"] == "Clinician"
    assert insights == [{"category": "MEDICAL_CONDITION", "type": "DX_NAME", "text": "hypertension"}]


def test_parse_note_links_evidence_and_skips_empty_sections():
    _, segments, _ = scribe.parse_transcript(scribe_transcript())
    note = scribe.parse_note(scribe_note(), "HISTORY_AND_PHYSICAL", segments)
    assert note["templateTitle"] == "History and physical"
    assert [s["title"] for s in note["sections"]] == ["Chief complaint", "Plan"]
    assert note["sections"][1]["items"][0]["evidence"] == [{"role": "Clinician", "text": "Start amlodipine."}]
    text = scribe.note_text(note)
    assert "CHIEF COMPLAINT\n- Blood pressure follow-up." in text and text.startswith("History and physical")


def test_numbered_participants():
    doc = scribe_transcript()
    doc["Conversation"]["TranscriptSegments"][3]["ParticipantDetails"]["ParticipantRole"] = "CLINICIAN_1"
    turns, _, _ = scribe.parse_transcript(doc)
    assert turns[-1]["role"] == "Clinician 2"


class RecordingTranscribe:
    def __init__(self):
        self.medical, self.scribe = [], []

    def start_medical_transcription_job(self, **kwargs):
        self.medical.append(kwargs)

    def start_medical_scribe_job(self, **kwargs):
        self.scribe.append(kwargs)


@pytest.mark.parametrize("channels, pronouns, settings, extra", [
    (1, None, {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 2}, {}),
    (2, "THEY_THEM", {"ChannelIdentification": True},
     {"ChannelDefinitions": [{"ChannelId": 0, "ParticipantRole": "CLINICIAN"},
                             {"ChannelId": 1, "ParticipantRole": "PATIENT"}],
      "MedicalScribeContext": {"PatientContext": {"Pronouns": "THEY_THEM"}}}),
])
def test_start_job_settings(channels, pronouns, settings, extra):
    client = RecordingTranscribe()
    scribe.start_job(client, name="job-1", bucket=BUCKET, media_uri="s3://b/k.wav", role_arn="arn:role",
                     template="PHYSICAL_SOAP", channels=channels, pronouns=pronouns)
    [call] = client.scribe
    assert call["Settings"] == {**settings, "ClinicalNoteGenerationSettings": {"NoteTemplate": "PHYSICAL_SOAP"}}
    assert {k: call[k] for k in extra} == extra
    assert call["DataAccessRoleArn"] == "arn:role" and call["OutputBucketName"] == BUCKET


def test_channel_identified_transcribe_output_maps_left_to_clinician():
    def item(t, w, kind="pronunciation"):
        base = {"type": kind, "alternatives": [{"confidence": "0.9", "content": w}]}
        return {**base, "start_time": str(t), "end_time": str(t + 0.3)} if kind == "pronunciation" else base
    doc = {"results": {"channel_labels": {"number_of_channels": 2, "channels": [
        {"channel_label": "ch_1", "items": [item(2.0, "My"), item(2.4, "knee"), item(0, ".", "punctuation")]},
        {"channel_label": "ch_0", "items": [item(0.0, "Hello"), item(0, ".", "punctuation"), item(4.0, "Rest")]},
    ]}, "items": []}}
    assert role_method(doc) == "channel"
    assert [(t["role"], t["text"]) for t in parse_turns(doc)] == [
        ("Clinician", "Hello."), ("Patient", "My knee."), ("Clinician", "Rest")]


@pytest.mark.parametrize("uri", [
    f"s3://{BUCKET}/job-1/summary.json", f"https://s3.us-east-1.amazonaws.com/{BUCKET}/job-1/summary.json",
    f"https://{BUCKET}.s3.us-east-1.amazonaws.com/job-1/summary.json"])
def test_parse_s3_uri(uri):
    assert parse_s3_uri(uri) == (BUCKET, "job-1/summary.json")


# --- start + watcher + assembly with moto --------------------------------------------------------

class FakeTranscribe(RecordingTranscribe):
    def __init__(self, status="COMPLETED", output_bucket=BUCKET):
        super().__init__()
        self.status, self.output_bucket = status, output_bucket

    def get_medical_scribe_job(self, MedicalScribeJobName):
        return {"MedicalScribeJob": {"MedicalScribeJobStatus": self.status, "MedicalScribeOutput": {
            "TranscriptFileUri": f"s3://{self.output_bucket}/{MedicalScribeJobName}/transcript.json",
            "ClinicalDocumentUri": f"s3://{self.output_bucket}/{MedicalScribeJobName}/summary.json"}}}


class FakeComprehendMedical:
    def detect_entities_v2(self, Text):
        return {"Entities": [], "ModelVersion": "t"}

    def infer_icd10_cm(self, Text):
        pos = Text.find("hypertension")
        return {"Entities": [] if pos < 0 else [cm_entity(
            "hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], begin=pos,
            codes=[("I10", "Essential (primary) hypertension", 0.9)])], "ModelVersion": "t"}

    def infer_rx_norm(self, Text):
        return {"Entities": [], "ModelVersion": "t"}


class FakeLambda:
    def __init__(self):
        self.invocations = []

    def invoke(self, **kwargs):
        self.invocations.append(json.loads(kwargs["Payload"]))


class Context:
    def get_remaining_time_in_millis(self):
        return 600_000


@pytest.fixture
def env(lambda_env, monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", BUCKET)
    monkeypatch.setenv("SCRIBE_ROLE_ARN", "arn:aws:iam::111111111111:role/scribe")
    monkeypatch.setenv("WATCHER_FUNCTION", "watcher-fn")
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        table = boto3.resource("dynamodb").create_table(
            TableName="test-drafts", KeySchema=[{"AttributeName": "draftId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "draftId", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        import analyze_transcript
        import scribe_watcher
        import start_transcription
        from shared.drafts_table import DraftsTable

        drafts, transcribe, lam = DraftsTable(table), FakeTranscribe(), FakeLambda()
        for module in (start_transcription, analyze_transcript, scribe_watcher):
            monkeypatch.setattr(module, "_table", lambda: drafts)
            monkeypatch.setattr(module, "_s3", lambda: s3)
        for module in (start_transcription, scribe_watcher):
            monkeypatch.setattr(module, "_transcribe", lambda: transcribe)
        for module in (analyze_transcript, scribe_watcher):
            monkeypatch.setattr(module, "_comprehend_medical", lambda: FakeComprehendMedical())
        monkeypatch.setattr(start_transcription, "_lambda", lambda: lam)
        monkeypatch.setattr(scribe_watcher, "POLL_SECONDS", 0)
        yield {"s3": s3, "table": table, "drafts": drafts, "transcribe": transcribe, "lambda": lam,
               "start": start_transcription, "analyze": analyze_transcript, "watcher": scribe_watcher}


def _wav(channels):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * channels * 160)
    return buf.getvalue()


def _upload(env, engine, channels=1, pronouns=None):
    item = {"draftId": DRAFT, "status": "UPLOADED", "patientId": PATIENT, "encounterId": DRAFT, "engine": engine,
            "noteTemplate": "HISTORY_AND_PHYSICAL", "createdAt": "2026-11-27T10:00:00Z"}
    if pronouns:
        item["pronouns"] = pronouns
    env["table"].put_item(Item=item)
    env["s3"].put_object(Bucket=BUCKET, Key=AUDIO_KEY, Body=_wav(channels))
    return env["start"].handler({"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": AUDIO_KEY}}}]}, None)


def _scribe_output(env, job):
    env["s3"].put_object(Bucket=BUCKET, Key=f"{job}/transcript.json", Body=json.dumps(scribe_transcript()))
    env["s3"].put_object(Bucket=BUCKET, Key=f"{job}/summary.json", Body=json.dumps(scribe_note()))


def _transcribe_output(env):
    doc = transcribe_output([("spk_0", "Any chest pain?"), ("spk_1", "No chest pain. I have hypertension.")])
    env["s3"].put_object(Bucket=BUCKET, Key=f"transcripts/{DRAFT}.json", Body=json.dumps(doc))
    return env["analyze"].handler({"Records": [{"s3": {"bucket": {"name": BUCKET},
                                                       "object": {"key": f"transcripts/{DRAFT}.json"}}}]}, None)


def _item(env):
    return env["table"].get_item(Key={"draftId": DRAFT})["Item"]


def _draft(env):
    return json.loads(env["s3"].get_object(Bucket=BUCKET, Key=f"drafts/{DRAFT}.json")["Body"].read())


def test_healthscribe_engine_end_to_end(env):
    assert _upload(env, "healthscribe", pronouns="SHE_HER") == ["started"]
    assert env["transcribe"].medical == []
    [job] = env["transcribe"].scribe
    assert job["MedicalScribeContext"] == {"PatientContext": {"Pronouns": "SHE_HER"}}
    [invocation] = env["lambda"].invocations
    assert invocation["mode"] == "pipeline" and invocation["jobName"] == job["MedicalScribeJobName"]
    assert _item(env)["pendingEngines"] == {"healthscribe"}

    _scribe_output(env, job["MedicalScribeJobName"])
    assert env["watcher"].handler(invocation, Context()) == "ready"

    item, draft = _item(env), _draft(env)
    assert item["status"] == "PENDING_REVIEW" and "pendingEngines" not in item
    assert draft["primaryEngine"] == "healthscribe" and "AWS HealthScribe" in draft["aiPipeline"]
    assert draft["roleMethod"].startswith("Participant roles identified by AWS HealthScribe")
    assert [t["role"] for t in draft["turns"]] == ["Clinician", "Patient", "Clinician"]
    kinds = [(p["id"], p["resourceType"], p["selected"]) for p in draft["proposed"]]
    assert ("documentreference-2", "DocumentReference", False) in kinds  # the note: flagged, not pre-selected
    note_item = next(p for p in draft["proposed"] if p.get("noteTemplate"))
    assert note_item["code"] == "34117-2" and note_item["flagReasons"] == ["AI_GENERATED_NOTE"]
    assert "comparison" not in draft
    stored_note = json.loads(env["s3"].get_object(Bucket=BUCKET, Key=f"notes/{DRAFT}/HISTORY_AND_PHYSICAL.json")["Body"].read())
    assert [s["name"] for s in stored_note["sections"]] == ["CHIEF_COMPLAINT", "PLAN"]


def test_both_engines_assemble_once_with_comparison(env):
    _upload(env, "both")
    assert len(env["transcribe"].medical) == 1 and len(env["transcribe"].scribe) == 1
    assert _transcribe_output(env) == ["waiting"]
    assert _item(env)["status"] == "ANALYZING" and _item(env)["pendingEngines"] == {"healthscribe"}

    [invocation] = env["lambda"].invocations
    _scribe_output(env, invocation["jobName"])
    assert env["watcher"].handler(invocation, Context()) == "ready"
    draft = _draft(env)
    assert set(draft["comparison"]) == {"transcribe", "healthscribe"}
    assert draft["comparison"]["transcribe"]["roleMethod"].startswith("First speaker")
    assert [p["code"] for p in draft["comparison"]["healthscribe"]["proposals"]] == ["I10"]
    assert draft["comparison"]["healthscribe"]["insights"][0]["text"] == "hypertension"
    # a duplicate watcher run does not assemble again
    assert env["watcher"].handler(invocation, Context()) == "skipped"


def test_stereo_audio_uses_channel_identification_for_both_engines(env):
    _upload(env, "both", channels=2)
    assert env["transcribe"].medical[0]["Settings"] == {"ChannelIdentification": True}
    assert env["transcribe"].scribe[0]["Settings"]["ChannelIdentification"] is True
    assert _item(env)["channels"] == 2


def test_failed_scribe_job_marks_draft_failed(env):
    _upload(env, "healthscribe")
    env["transcribe"].status = "FAILED"
    assert env["watcher"].handler(env["lambda"].invocations[0], Context()) == "failed"
    item = _item(env)
    assert (item["status"], item["errorCode"]) == ("FAILED", "SCRIBE_JOB_FAILED")


def test_watcher_only_reads_its_own_job_output(env):
    _upload(env, "healthscribe")
    env["transcribe"].output_bucket = "someone-elses-bucket"
    env["watcher"].handler(env["lambda"].invocations[0], Context())
    assert _item(env)["errorCode"] == "SCRIBE_UNEXPECTED_OUTPUT"


def test_note_mode_adds_a_note_and_clears_pending(env):
    _upload(env, "healthscribe")
    first = env["lambda"].invocations[0]
    _scribe_output(env, first["jobName"])
    env["watcher"].handler(first, Context())
    assert env["drafts"].add_pending_note(DRAFT, "PHYSICAL_SOAP") is not None
    assert env["drafts"].add_pending_note(DRAFT, "PHYSICAL_SOAP") is None  # already generating
    job = scribe.job_name("voice-to-fhir-test", DRAFT, "PHYSICAL_SOAP", 7)
    _scribe_output(env, job)
    event = {"draftId": DRAFT, "jobName": job, "template": "PHYSICAL_SOAP", "mode": "note"}
    assert env["watcher"].handler(event, Context()) == "note_ready"
    assert "pendingNotes" not in _item(env)
    note = json.loads(env["s3"].get_object(Bucket=BUCKET, Key=f"notes/{DRAFT}/PHYSICAL_SOAP.json")["Body"].read())
    assert note["templateTitle"] == "SOAP (physical health)"
