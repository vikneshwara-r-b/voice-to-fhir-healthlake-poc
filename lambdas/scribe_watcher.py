"""Watch one AWS HealthScribe job (invoked asynchronously), then process its output.

HealthScribe has no output-key parameter, so instead of an S3 event this function polls
GetMedicalScribeJob and reads the TranscriptFileUri / ClinicalDocumentUri it reports.

Event: {"draftId", "jobName", "template", "mode": "pipeline" | "note"}
- pipeline: store the note, run Comprehend Medical on the HealthScribe transcript and finish the
  "healthscribe" engine for the draft (see shared/engines.py);
- note: store an additional note (another template) for a draft already waiting for review.
"""

import functools
import os
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from shared import drafts_table as dt
from shared.engines import EngineError, finish_engine, get_json, note_key, parse_s3_uri, put_json
from shared.log import log
from shared.nlp import analyze
from shared.scribe import parse_note, parse_transcript

POLL_SECONDS = 15
STOP_POLLING_MS = 180_000  # leave time to process the output


@functools.cache
def _s3():
    return boto3.client("s3")


@functools.cache
def _transcribe():
    return boto3.client("transcribe")


@functools.cache
def _comprehend_medical():
    return boto3.client("comprehendmedical", config=Config(retries={"mode": "standard", "max_attempts": 5}))


@functools.cache
def _table() -> dt.DraftsTable:
    return dt.DraftsTable(boto3.resource("dynamodb").Table(os.environ["DRAFTS_TABLE"]))


def _bucket() -> str:
    return os.environ["DATA_BUCKET"]


def thresholds() -> dict:
    return {"threshold": float(os.environ.get("CONFIDENCE_THRESHOLD", "0.80")),
            "code_floor": float(os.environ.get("CODE_SCORE_FLOOR", "0.30"))}


def wait_for_job(name: str, remaining_ms) -> dict:
    while True:
        job = _transcribe().get_medical_scribe_job(MedicalScribeJobName=name)["MedicalScribeJob"]
        status = job["MedicalScribeJobStatus"]
        if status == "COMPLETED":
            return job
        if status == "FAILED":
            raise EngineError("SCRIBE_JOB_FAILED")
        if remaining_ms() < STOP_POLLING_MS:
            raise EngineError("SCRIBE_TIMEOUT")
        time.sleep(POLL_SECONDS)


def _read_output(uri: str, name: str) -> dict:
    bucket, key = parse_s3_uri(uri)
    if bucket != _bucket() or not key.startswith(name):  # only read this job's own output
        raise EngineError("SCRIBE_UNEXPECTED_OUTPUT")
    return get_json(_s3(), bucket, key)


def process(event: dict, remaining_ms) -> str:
    draft_id, name, template, mode = event["draftId"], event["jobName"], event["template"], event["mode"]
    job = wait_for_job(name, remaining_ms)
    output = job["MedicalScribeOutput"]
    turns, segments, insights = parse_transcript(_read_output(output["TranscriptFileUri"], name))
    note = parse_note(_read_output(output["ClinicalDocumentUri"], name), template, segments)
    put_json(_s3(), _bucket(), note_key(draft_id, template), {**note, "jobName": name, "createdAt": dt.utc_now()})
    log("scribe_note_stored", draftId=draft_id, template=template, sections=len(note["sections"]), mode=mode)

    if mode == "note":
        _table().finish_pending_note(draft_id, template)
        return "note_ready"
    if not turns:
        raise EngineError("EMPTY_TRANSCRIPT")
    if _table().transition(draft_id, dt.ANALYZING, allowed_from=(dt.TRANSCRIBING, dt.ANALYZING)) is None:
        log("duplicate_or_unknown_ignored", draftId=draft_id)
        return "skipped"
    channel = bool(job.get("ChannelDefinitions"))
    result = {"engine": "healthscribe", "turns": turns, "nlp": analyze(turns, _comprehend_medical()),
              "insights": insights, "note": note, "roleMethod": "channel" if channel else "healthscribe"}
    return finish_engine(s3=_s3(), bucket=_bucket(), table=_table(), draft_id=draft_id, engine="healthscribe",
                         result=result, thresholds=thresholds())


def handler(event, context):
    draft_id, template, mode = event.get("draftId"), event.get("template"), event.get("mode")
    try:
        return process(event, context.get_remaining_time_in_millis)
    except (EngineError, ClientError, Exception) as exc:  # codes only, never messages
        code = exc.code if isinstance(exc, EngineError) else (
            exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__)
        if mode == "note":
            _table().finish_pending_note(draft_id, template, error_code=code)
        else:
            _table().fail(draft_id, code if code.startswith("SCRIBE_") else f"SCRIBE_{code}")
        log("scribe_failed", draftId=draft_id, mode=mode, errorCode=code)
        return "failed"
