"""S3 transcripts/*.json event (Amazon Transcribe Medical output) -> Comprehend Medical -> engine result.

The draft is assembled (drafts/ and nlp/ JSON, status PENDING_REVIEW) once every engine chosen
for it has finished; see shared/engines.py.
"""

import functools
import json
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from shared import drafts_table as dt
from shared.engines import EngineError, finish_engine
from shared.keys import object_key, parse_transcript_key, transcript_key
from shared.log import log
from shared.nlp import analyze
from shared.transcript_parser import parse_turns, role_method

RETRYABLE = {"TooManyRequestsException", "ThrottlingException", "ServiceUnavailableException",
             "InternalServerException", "SlowDown"}


class AnalysisError(EngineError):
    """Kept for compatibility: a short, PHI-free error code."""


@functools.cache
def _s3():
    return boto3.client("s3")


@functools.cache
def _comprehend_medical():
    return boto3.client("comprehendmedical", config=Config(retries={"mode": "standard", "max_attempts": 5}))


@functools.cache
def _table() -> dt.DraftsTable:
    return dt.DraftsTable(boto3.resource("dynamodb").Table(os.environ["DRAFTS_TABLE"]))


def thresholds() -> dict:
    return {"threshold": float(os.environ.get("CONFIDENCE_THRESHOLD", "0.80")),
            "code_floor": float(os.environ.get("CODE_SCORE_FLOOR", "0.30"))}


def process(bucket: str, draft_id: str) -> dict:
    transcript = json.loads(_s3().get_object(Bucket=bucket, Key=transcript_key(draft_id))["Body"].read())
    turns = parse_turns(transcript)
    if not turns:
        raise AnalysisError("EMPTY_TRANSCRIPT")
    return {"engine": "transcribe", "turns": turns, "nlp": analyze(turns, _comprehend_medical()),
            "roleMethod": role_method(transcript)}


def handle_record(record: dict) -> str:
    bucket = record["s3"]["bucket"]["name"]
    draft_id = parse_transcript_key(object_key(record))
    if draft_id is None:
        log("ignored_key", reason="not a transcripts/{draftId}.json key")
        return "ignored"

    item = _table().transition(draft_id, dt.ANALYZING, allowed_from=(dt.TRANSCRIBING, dt.ANALYZING))
    if item is None:
        log("duplicate_or_unknown_ignored", draftId=draft_id)
        return "skipped"

    try:
        result = process(bucket, draft_id)
        return finish_engine(s3=_s3(), bucket=bucket, table=_table(), draft_id=draft_id, engine="transcribe",
                             result=result, thresholds=thresholds())
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in RETRYABLE:
            log("analyze_retry", draftId=draft_id, errorCode=code)
            raise  # status stays ANALYZING, which allows the retry
        _table().fail(draft_id, f"ANALYZE_{code}")
        log("analyze_failed", draftId=draft_id, errorCode=code)
        return "failed"
    except Exception as exc:  # never put exception messages in the table or logs
        code = exc.code if isinstance(exc, EngineError) else type(exc).__name__
        _table().fail(draft_id, f"ANALYZE_{code}")
        log("analyze_failed", draftId=draft_id, errorCode=code)
        return "failed"


def handler(event, _context):
    return [handle_record(r) for r in event.get("Records", [])]
