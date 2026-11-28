"""HTTP API (payload v2) router behind the Cognito JWT authorizer.

GET  /patients              synthetic patients from HealthLake (+ active conditions)
GET  /patients/{id}/summary EHR-style chart: problems, medications, vitals, labs, encounters, notes
GET  /patients/{id}/fhir?type=Condition   read-only HealthLake request for this patient; returns the
                            request URL, HTTP status, timing, HealthLake request id and the raw FHIR JSON
PUT  /patients/{id}/encounters/{encounterId}  {start, end?}: update the encounter date/time (+ Provenance)
GET  /samples               bundled sample audio under samples/
POST /process               {patientId, sampleKey}: new draft, copy sample to audio/... (starts the pipeline)
POST /process/upload        ?patientId=..., raw audio body (<= 4 MB): new draft from a synthetic recording
GET  /drafts                recent drafts (status only)
GET  /drafts/{id}           status, and once analysed: transcript turns, NLP entities, proposed resources
POST /drafts/{id}/notes     {template}: generate another AWS HealthScribe note for a draft under review
POST /drafts/{id}/notes/{template}/use  make that note the proposed clinical-note DocumentReference
POST /drafts/{id}/items     add a Condition or MedicationStatement by hand (named fields, not raw FHIR)
PUT  /drafts/{id}/items/{itemId}  edit a proposed resource (named fields)
POST /drafts/{id}/reject    {feedback} (required): PENDING_REVIEW -> REJECTED
POST /drafts/{id}/approve    {resourceIds, feedback?}: write the selected resources + Provenance to HealthLake

Review feedback is free text a clinician typed, so it is treated like PHI: stored only in the
draft JSON in S3, never in DynamoDB, logs or HealthLake.

Responses never echo request bodies; logs carry ids, routes, statuses and counts only.
"""

import base64
import functools
import json
import os
import re
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from shared import drafts_table as dt
from shared.approve import ApproveError, approve
from shared.draft_edit import EditError, add_item, edit_item, parse_feedback, set_review
from shared.encounters import EncounterError, parse_instant, update_period
from shared import scribe
from shared.engines import note_key
from shared.fhir_mapper import note_reference
from shared.keys import parse_audio_key
from shared.healthlake import HealthLakeClient, HealthLakeError
from shared.keys import AUDIO_EXTENSIONS, draft_key, is_draft_id, is_fhir_id, nlp_key
from shared.log import log
from shared.chart import build_chart
from shared.review import entity_rows, patient_summary

SAMPLE_KEY = re.compile(rf"^samples/[A-Za-z0-9_\-.]{{1,100}}\.({'|'.join(AUDIO_EXTENSIONS)})$")
REVIEWABLE = (dt.PENDING_REVIEW, dt.APPROVED, dt.WRITTEN, dt.REJECTED)
MAX_PATIENTS = 20
MAX_DRAFTS = 50
AUDIO_URL_SECONDS = 3600
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # Lambda request limit is 6 MB and the body arrives base64-encoded
UPLOAD_TYPES = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/vnd.wave": "wav",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a",
}
CONTENT_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "m4a": "audio/mp4"}
PUBLIC_FIELDS = ("draftId", "patientId", "encounterId", "status", "errorCode", "createdAt", "updatedAt",
                 "turnCount", "entityCount", "proposedCount", "flaggedCount", "excludedCount", "healthLakeIds",
                 "source", "hasFeedback", "engine", "engines", "noteTemplate", "pendingEngines", "pendingNotes",
                 "channels", "lastNoteError", "pronouns")
ENGINE_CHOICES = ("transcribe", "healthscribe", "both")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra


# --- clients (replaced in tests) -------------------------------------------------------------

@functools.cache
def _s3():
    return boto3.client("s3")


@functools.cache
def _s3_presign():
    """Virtual-hosted regional URLs (https://<bucket>.s3.<region>.amazonaws.com/...), the host the CSP allows."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))


def _audio_url(key: str) -> str:
    return _s3_presign().generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=AUDIO_URL_SECONDS)


@functools.cache
def _lambda():
    return boto3.client("lambda")


@functools.cache
def _transcribe():
    return boto3.client("transcribe")


@functools.cache
def _table() -> dt.DraftsTable:
    return dt.DraftsTable(boto3.resource("dynamodb").Table(os.environ["DRAFTS_TABLE"]))


@functools.cache
def _healthlake() -> HealthLakeClient:
    session = boto3.Session()
    return HealthLakeClient(os.environ["HEALTHLAKE_DATASTORE_ID"], session.region_name, session.get_credentials())


def _bucket() -> str:
    return os.environ["DATA_BUCKET"]


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --- helpers ---------------------------------------------------------------------------------

def _json_default(value):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(type(value).__name__)


def _response(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=_json_default),
    }


def _body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raise ApiError(400, "BAD_REQUEST", "Expected a JSON body.")
    try:
        body = json.loads(raw)
    except ValueError:
        raise ApiError(400, "BAD_REQUEST", "Body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise ApiError(400, "BAD_REQUEST", "Body must be a JSON object.")
    return body


def _draft_id(event: dict) -> str:
    draft_id = (event.get("pathParameters") or {}).get("id", "")
    if not is_draft_id(draft_id):
        raise ApiError(400, "BAD_DRAFT_ID", "Invalid draft id.")
    return draft_id


def _public(item: dict) -> dict:
    return {k: item[k] for k in PUBLIC_FIELDS if k in item}


def _read_json(key: str) -> dict:
    return json.loads(_s3().get_object(Bucket=_bucket(), Key=key)["Body"].read())


def _save_draft(draft_id: str, draft: dict) -> None:
    _s3().put_object(Bucket=_bucket(), Key=draft_key(draft_id), Body=json.dumps(draft).encode("utf-8"),
                     ContentType="application/json")


def _edit_error(exc: EditError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, **({"problems": exc.problems} if exc.problems else {}))


# --- routes ----------------------------------------------------------------------------------

def list_patients(event, user):
    hl = _healthlake()
    patients = hl.search("Patient", {"_count": str(MAX_PATIENTS)})
    result = []
    for patient in patients[:MAX_PATIENTS]:
        conditions = hl.search("Condition", {
            "subject": f"Patient/{patient['id']}", "clinical-status": "active", "_count": "100"})
        result.append(patient_summary(patient, conditions, _today()))
    result.sort(key=lambda p: p["name"])
    return 200, {"patients": result}


CHART_TYPES = ("Condition", "MedicationRequest", "MedicationStatement", "Observation", "Encounter", "DocumentReference")


def patient_chart(event, user):
    patient_id = (event.get("pathParameters") or {}).get("id", "")
    if not is_fhir_id(patient_id):
        raise ApiError(400, "BAD_PATIENT_ID", "Invalid patient id.")
    hl = _healthlake()
    try:
        patient = hl.read("Patient", patient_id)
    except HealthLakeError as exc:
        if exc.status == 404:
            raise ApiError(404, "UNKNOWN_PATIENT", "Patient not found in HealthLake.") from None
        raise
    params = {"subject": f"Patient/{patient_id}", "_count": "100"}
    with ThreadPoolExecutor(max_workers=len(CHART_TYPES)) as pool:
        results = list(pool.map(lambda kind: hl.search(kind, params), CHART_TYPES))
    return 200, build_chart(patient, dict(zip(CHART_TYPES, results)), _today())


FHIR_VIEW_TYPES = ("Patient", *CHART_TYPES)


def _patient_id(event) -> str:
    patient_id = (event.get("pathParameters") or {}).get("id", "")
    if not is_fhir_id(patient_id):
        raise ApiError(400, "BAD_PATIENT_ID", "Invalid patient id.")
    return patient_id


def fhir_view(event, user):
    """One read-only HealthLake request, passed back unmodified (with request details) as evidence."""
    patient_id = _patient_id(event)
    kind = (event.get("queryStringParameters") or {}).get("type", "Patient")
    if kind not in FHIR_VIEW_TYPES:
        raise ApiError(400, "BAD_TYPE", f"type must be one of: {', '.join(FHIR_VIEW_TYPES)}.")
    hl = _healthlake()
    path, params = (f"Patient/{patient_id}", None) if kind == "Patient" else (
        kind, {"subject": f"Patient/{patient_id}", "_count": "100"})
    url = hl.base_url + path + ("?" + urllib.parse.urlencode(params) if params else "")
    started = time.monotonic()
    try:
        status, body, headers = hl.request("GET", path, params=params)
    except HealthLakeError as exc:
        status, body, headers = exc.status, {"issue": [{"code": c} for c in exc.issue_codes]}, {}
    request_id = next((v for k, v in headers.items() if k.lower() == "x-amzn-requestid"), None)
    log("fhir_view", patientId=patient_id, resourceType=kind, status=status)
    return 200, {"request": {"method": "GET", "url": url, "signedWith": "AWS SigV4 (service healthlake)"},
                 "status": status, "durationMs": round((time.monotonic() - started) * 1000),
                 "requestId": request_id, "response": body}


def update_encounter(event, user):
    patient_id = _patient_id(event)
    encounter_id = (event.get("pathParameters") or {}).get("encounterId", "")
    if not is_fhir_id(encounter_id):
        raise ApiError(400, "BAD_ENCOUNTER_ID", "Invalid encounter id.")
    body = _body(event)
    try:
        start = parse_instant(body.get("start"), "start")
        end = parse_instant(body.get("end"), "end", required=False)
        result = update_period(healthlake=_healthlake(), patient_id=patient_id, encounter_id=encounter_id,
                               start=start, end=end, user=user, now=dt.utc_now())
    except EncounterError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from None
    log("encounter_updated", patientId=patient_id, encounterId=encounter_id, versionId=result["versionId"])
    return 200, {"versionId": result["versionId"], "provenance": result["provenance"],
                 "period": result["encounter"]["period"]}


def list_samples(event, user):
    response = _s3().list_objects_v2(Bucket=_bucket(), Prefix="samples/", MaxKeys=100)
    samples = [
        {"key": o["Key"], "name": o["Key"].split("/", 1)[1], "sizeBytes": o["Size"], "url": _audio_url(o["Key"])}
        for o in response.get("Contents", []) if SAMPLE_KEY.match(o["Key"])
    ]
    return 200, {"samples": sorted(samples, key=lambda s: s["name"])}


def _require_patient(patient_id: str) -> None:
    if not is_fhir_id(patient_id):
        raise ApiError(400, "BAD_PATIENT_ID", "Invalid patient id.")
    try:
        _healthlake().read("Patient", patient_id)
    except HealthLakeError as exc:
        if exc.status == 404:
            raise ApiError(400, "UNKNOWN_PATIENT", "Patient not found in HealthLake.") from None
        raise


def _new_draft(patient_id: str, ext: str, name: str, user: dict, extra: dict) -> tuple[str, str]:
    draft_id = str(uuid.uuid4())
    audio_key = f"audio/{patient_id}/{draft_id}/{name}.{ext}"
    created = _table().create(draft_id, dt.UPLOADED, {
        "patientId": patient_id, "encounterId": draft_id, "audioKey": audio_key, "createdBy": user["sub"], **extra,
    })
    if created is None:  # uuid collision: practically impossible
        raise ApiError(409, "DRAFT_EXISTS", "Try again.")
    return draft_id, audio_key


def _engine_options(source: dict) -> dict:
    """engine (transcribe | healthscribe | both), HealthScribe note template, optional patient pronouns."""
    engine = source.get("engine") or "transcribe"
    template = source.get("noteTemplate") or "HISTORY_AND_PHYSICAL"
    pronouns = source.get("pronouns") or None
    if engine not in ENGINE_CHOICES:
        raise ApiError(400, "BAD_ENGINE", "engine must be transcribe, healthscribe or both.")
    if template not in scribe.TEMPLATES:
        raise ApiError(400, "BAD_TEMPLATE", "Unknown HealthScribe note template.")
    if pronouns is not None and pronouns not in scribe.PRONOUNS:
        raise ApiError(400, "BAD_PRONOUNS", "pronouns must be HE_HIM, SHE_HER or THEY_THEM.")
    return {"engine": engine, "noteTemplate": template, **({"pronouns": pronouns} if pronouns else {})}


def process(event, user):
    body = _body(event)
    options = _engine_options(body)
    patient_id, sample_key = body.get("patientId", ""), body.get("sampleKey", "")
    if not SAMPLE_KEY.match(sample_key):
        raise ApiError(400, "BAD_SAMPLE_KEY", "Choose one of the listed sample files.")
    _require_patient(patient_id)
    draft_id, audio_key = _new_draft(patient_id, sample_key.rsplit(".", 1)[1], "consult", user,
                                     {"source": "sample", "sampleKey": sample_key, **options})
    try:
        _s3().copy_object(Bucket=_bucket(), Key=audio_key,
                          CopySource={"Bucket": _bucket(), "Key": sample_key})
    except ClientError as exc:
        _table().fail(draft_id, "COPY_FAILED")
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise ApiError(400, "BAD_SAMPLE_KEY", "Sample not found.") from None
        raise
    log("process_started", draftId=draft_id, source="sample")
    return 202, {"draftId": draft_id, "status": dt.UPLOADED}


def _looks_like(ext: str, data: bytes) -> bool:
    if ext == "wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if ext == "mp3":
        return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if ext == "flac":
        return data[:4] == b"fLaC"
    return data[4:8] == b"ftyp"  # m4a / mp4


def upload(event, user):
    """Raw audio body (API Gateway delivers it base64-encoded). Synthetic recordings only."""
    query = event.get("queryStringParameters") or {}
    patient_id = query.get("patientId", "")
    options = _engine_options(query)
    content_type = (event.get("headers") or {}).get("content-type", "").split(";")[0].strip().lower()
    ext = UPLOAD_TYPES.get(content_type)
    if ext is None:
        raise ApiError(400, "UNSUPPORTED_TYPE", "Upload a WAV, MP3, FLAC or M4A file.")
    if not event.get("isBase64Encoded") or not event.get("body"):
        raise ApiError(400, "EMPTY_FILE", "The file is empty.")
    try:
        data = base64.b64decode(event["body"], validate=True)
    except ValueError:
        raise ApiError(400, "BAD_REQUEST", "Could not read the file.") from None
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError(400, "FILE_TOO_LARGE", "The file is larger than 4 MB.")
    if not _looks_like(ext, data):
        raise ApiError(400, "NOT_AUDIO", f"The file does not look like {ext.upper()} audio.")
    if ext == "wav" and len(data) >= 28 and int.from_bytes(data[24:28], "little") < 16000:
        raise ApiError(400, "LOW_SAMPLE_RATE", "WAV audio must be recorded at 16 kHz or higher.")
    _require_patient(patient_id)

    draft_id, audio_key = _new_draft(patient_id, ext, "upload", user, {"source": "upload", **options})
    _s3().put_object(Bucket=_bucket(), Key=audio_key, Body=data, ContentType=CONTENT_TYPES[ext])
    log("process_started", draftId=draft_id, source="upload", sizeBytes=len(data))
    return 202, {"draftId": draft_id, "status": dt.UPLOADED}


def list_drafts(event, user):
    items = sorted(_table().scan(), key=lambda i: i.get("createdAt", ""), reverse=True)
    return 200, {"drafts": [_public(i) for i in items[:MAX_DRAFTS]]}


def _check_transcription_job(item: dict) -> dict:
    """A failed Transcribe job writes no transcript; detect it here and mark FAILED."""
    try:
        job = _transcribe().get_medical_transcription_job(MedicalTranscriptionJobName=item["jobName"])
    except ClientError as exc:
        log("job_check_skipped", draftId=item["draftId"], errorCode=exc.response["Error"]["Code"])
        return item
    if job["MedicalTranscriptionJob"]["TranscriptionJobStatus"] == "FAILED":
        log("transcription_failed", draftId=item["draftId"])
        return _table().fail(item["draftId"], "TRANSCRIBE_JOB_FAILED") or item
    return item


def get_draft(event, user):
    draft_id = _draft_id(event)
    item = _table().get(draft_id)
    if item is None:
        raise ApiError(404, "NOT_FOUND", "Draft not found.")
    if item.get("status") == dt.TRANSCRIBING and item.get("jobName"):
        item = _check_transcription_job(item)

    result = {"draft": _public(item)}
    if item.get("audioKey"):
        result["audioUrl"] = _audio_url(item["audioKey"])
    if item.get("status") in REVIEWABLE:
        draft = _read_json(draft_key(draft_id))
        result.update(
            turns=draft["turns"],
            proposed=draft["proposed"],
            excluded=draft["excluded"],
            entities=entity_rows(_read_json(nlp_key(draft_id))),
            thresholds={"confidence": draft.get("confidenceThreshold"),
                        "codeScoreFloor": draft.get("codeScoreFloor")},
            review=draft.get("review"),
            primaryEngine=draft.get("primaryEngine", "transcribe"),
            roleMethod=draft.get("roleMethod"),
            comparison=draft.get("comparison"),
            noteTemplate=draft.get("noteTemplate"),
            notes=_load_notes(draft_id),
        )
    return 200, result


def _load_notes(draft_id: str) -> dict:
    listing = _s3().list_objects_v2(Bucket=_bucket(), Prefix=f"notes/{draft_id}/", MaxKeys=20)
    notes = {}
    for obj in listing.get("Contents", []):
        note = _read_json(obj["Key"])
        notes[note["template"]] = note
    return notes


def generate_note(event, user):
    """Run AWS HealthScribe again on the draft's audio with another note template."""
    draft_id = _draft_id(event)
    template = _body(event).get("template")
    if template not in scribe.TEMPLATES:
        raise ApiError(400, "BAD_TEMPLATE", "Unknown HealthScribe note template.")
    item = _table().get(draft_id)
    if item is None:
        raise ApiError(404, "NOT_FOUND", "Draft not found.")
    audio = parse_audio_key(item.get("audioKey", ""))
    if item.get("status") != dt.PENDING_REVIEW or audio is None:
        raise ApiError(409, "NOT_PENDING_REVIEW", "Notes can be generated only for drafts waiting for review.")
    if _table().add_pending_note(draft_id, template) is None:
        raise ApiError(409, "NOTE_IN_PROGRESS", "That note is already being generated.")
    name = scribe.job_name(os.environ["JOB_PREFIX"], draft_id, template, int(time.time()))
    try:
        scribe.start_job(_transcribe(), name=name, bucket=_bucket(), media_uri=f"s3://{_bucket()}/{audio.key}",
                         role_arn=os.environ["SCRIBE_ROLE_ARN"], template=template,
                         channels=int(item.get("channels", 1)), pronouns=item.get("pronouns"))
        _lambda().invoke(FunctionName=os.environ["WATCHER_FUNCTION"], InvocationType="Event", Payload=json.dumps(
            {"draftId": draft_id, "jobName": name, "template": template, "mode": "note"}).encode())
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        _table().finish_pending_note(draft_id, template, error_code=code)
        raise ApiError(502, "SCRIBE_START_FAILED", f"AWS HealthScribe could not start ({code}).") from None
    log("note_requested", draftId=draft_id, template=template)
    return 202, {"template": template, "status": "GENERATING"}


def use_note(event, user):
    draft_id = _draft_id(event)
    template = (event.get("pathParameters") or {}).get("template", "")
    if template not in scribe.TEMPLATES:
        raise ApiError(400, "BAD_TEMPLATE", "Unknown HealthScribe note template.")
    draft = _editable_draft(draft_id)
    try:
        note = _read_json(note_key(draft_id, template))
    except ClientError:
        raise ApiError(404, "NOTE_NOT_FOUND", "That note has not been generated.") from None
    ctx = {"patientId": draft["patientId"], "encounterId": draft["encounterId"], "now": dt.utc_now()}
    item = note_reference(note, ctx)
    existing = next((p for p in draft["proposed"] if p.get("noteTemplate")), None)
    if existing:
        item["id"] = existing["id"]
        draft["proposed"][draft["proposed"].index(existing)] = item
    else:
        used = [int(p["id"].rsplit("-", 1)[1]) for p in draft["proposed"] if p["id"].startswith("documentreference-")]
        item["id"] = f"documentreference-{max(used, default=0) + 1}"
        draft["proposed"].insert(1, item)
    draft["noteTemplate"] = template
    _save_draft(draft_id, draft)
    log("note_selected", draftId=draft_id, template=template, itemId=item["id"])
    return 200, {"item": item}


def _editable_draft(draft_id: str) -> dict:
    item = _table().get(draft_id)
    if item is None:
        raise ApiError(404, "NOT_FOUND", "Draft not found.")
    if item.get("status") != dt.PENDING_REVIEW:
        raise ApiError(409, "NOT_PENDING_REVIEW", "Only drafts waiting for review can be changed.")
    return _read_json(draft_key(draft_id))


def add_draft_item(event, user):
    draft_id = _draft_id(event)
    draft = _editable_draft(draft_id)
    try:
        item = add_item(draft, _body(event), dt.utc_now())
    except EditError as exc:
        raise _edit_error(exc) from None
    _save_draft(draft_id, draft)
    log("draft_item_added", draftId=draft_id, itemId=item["id"], resourceType=item["resourceType"])
    return 201, {"item": item}


def edit_draft_item(event, user):
    draft_id = _draft_id(event)
    item_id = (event.get("pathParameters") or {}).get("itemId", "")
    draft = _editable_draft(draft_id)
    try:
        item = edit_item(draft, item_id, _body(event))
    except EditError as exc:
        raise _edit_error(exc) from None
    _save_draft(draft_id, draft)
    log("draft_item_edited", draftId=draft_id, itemId=item["id"], resourceType=item["resourceType"])
    return 200, {"item": item}


def reject_draft(event, user):
    draft_id = _draft_id(event)
    try:
        feedback = parse_feedback(_body(event), required=True)
    except EditError as exc:
        raise _edit_error(exc) from None
    now = dt.utc_now()
    item = _table().transition(
        draft_id, dt.REJECTED, allowed_from=(dt.PENDING_REVIEW,),
        attrs={"reviewedBy": user["sub"], "reviewedAt": now, "hasFeedback": True},
    )
    if item is None:
        if _table().get(draft_id) is None:
            raise ApiError(404, "NOT_FOUND", "Draft not found.")
        raise ApiError(409, "NOT_PENDING_REVIEW", "Only drafts waiting for review can be rejected.")
    try:
        draft = _read_json(draft_key(draft_id))
        set_review(draft, dt.REJECTED, feedback, user["sub"], now)
        _save_draft(draft_id, draft)
    except Exception as exc:  # put the draft back so the clinician can retry with the feedback
        _table().transition(draft_id, dt.PENDING_REVIEW, allowed_from=(dt.REJECTED,), attrs={"hasFeedback": False})
        log("reject_feedback_not_saved", draftId=draft_id, errorType=type(exc).__name__)
        raise ApiError(502, "FEEDBACK_NOT_SAVED", "Could not save your feedback; the draft is still waiting for review.") from None
    log("draft_rejected", draftId=draft_id)
    return 200, {"draft": _public(item)}


def approve_draft(event, user):
    draft_id = _draft_id(event)
    body = _body(event)
    try:
        feedback = parse_feedback(body, required=False)
        encounter_start = parse_instant(body.get("encounterStart"), "encounterStart", required=False)
    except EditError as exc:
        raise _edit_error(exc) from None
    except EncounterError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from None

    def save_review(draft, text):
        set_review(draft, dt.APPROVED, text, user["sub"], dt.utc_now())
        _save_draft(draft_id, draft)

    try:
        result = approve(
            draft_id=draft_id,
            requested_ids=body.get("resourceIds"),
            user=user,
            table=_table(),
            read_draft=lambda d: _read_json(draft_key(d)),
            healthlake=_healthlake(),
            feedback=feedback,
            save_review=save_review,
            encounter_start=encounter_start,
        )
    except ApproveError as exc:
        log("approve_failed", draftId=draft_id, status=exc.status, errorCode=exc.code,
            writtenCount=exc.extra.get("writtenCount"))
        return exc.status, {"error": exc.code, "message": exc.message, **exc.extra}
    log("draft_written", draftId=draft_id, writtenCount=len(result["written"]))
    return 200, {"draft": _public(result["draft"]), "written": result["written"]}


ROUTES = {
    "GET /patients": list_patients,
    "GET /patients/{id}/summary": patient_chart,
    "GET /patients/{id}/fhir": fhir_view,
    "PUT /patients/{id}/encounters/{encounterId}": update_encounter,
    "GET /samples": list_samples,
    "POST /process": process,
    "POST /process/upload": upload,
    "GET /drafts": list_drafts,
    "GET /drafts/{id}": get_draft,
    "POST /drafts/{id}/notes": generate_note,
    "POST /drafts/{id}/notes/{template}/use": use_note,
    "POST /drafts/{id}/items": add_draft_item,
    "PUT /drafts/{id}/items/{itemId}": edit_draft_item,
    "POST /drafts/{id}/reject": reject_draft,
    "POST /drafts/{id}/approve": approve_draft,
}


def handler(event, _context):
    route = event.get("routeKey", "")
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    if not claims.get("sub"):
        return _response(401, {"error": "UNAUTHORIZED", "message": "Sign in again."})
    fn = ROUTES.get(route)
    if fn is None:
        return _response(404, {"error": "NOT_FOUND", "message": "Unknown route."})
    try:
        status, body = fn(event, {"sub": claims["sub"], "iss": claims.get("iss"), "name": claims.get("name")})
    except ApiError as exc:
        log("api_error", route=route, status=exc.status, errorCode=exc.code)
        return _response(exc.status, {"error": exc.code, "message": exc.message, **exc.extra})
    except HealthLakeError as exc:
        log("healthlake_error", route=route, status=exc.status, issues=exc.issue_codes)
        return _response(502, {"error": "HEALTHLAKE_ERROR", "message": "HealthLake request failed."})
    except Exception as exc:  # no message: it could contain data
        log("api_unhandled", route=route, errorType=type(exc).__name__)
        return _response(500, {"error": "INTERNAL", "message": "Something went wrong."})
    log("api_ok", route=route, status=status)
    return _response(status, body)
