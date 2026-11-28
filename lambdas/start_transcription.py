"""S3 audio/ event -> start the AI engine(s) chosen for the draft.

- "transcribe": Amazon Transcribe Medical batch job (PRIMARYCARE, CONVERSATION); output under
  transcripts/ triggers analyze_transcript.
- "healthscribe": AWS HealthScribe job (roles, sections, clinical note), watched by scribe_watcher.
- "both": both, for a side-by-side comparison.
Stereo WAV (clinician left, patient right) uses channel identification instead of speaker labels.

Idempotent: the DynamoDB transition only allows UPLOADED/TRANSCRIBING (or a new draft), and the
deterministic job names turn a repeated start into a ConflictException.
"""

import functools
import json
import os

import boto3
from botocore.exceptions import ClientError

from shared import drafts_table as dt
from shared import scribe
from shared.keys import job_name, object_key, parse_audio_key, transcript_key
from shared.log import log

RETRYABLE = {"LimitExceededException", "ThrottlingException", "InternalFailureException"}
ENGINES = {"transcribe": ["transcribe"], "healthscribe": ["healthscribe"], "both": ["transcribe", "healthscribe"]}


@functools.cache
def _transcribe():
    return boto3.client("transcribe")


@functools.cache
def _s3():
    return boto3.client("s3")


@functools.cache
def _lambda():
    return boto3.client("lambda")


@functools.cache
def _table() -> dt.DraftsTable:
    return dt.DraftsTable(boto3.resource("dynamodb").Table(os.environ["DRAFTS_TABLE"]))


def channel_count(bucket: str, audio) -> int:
    """WAV header bytes 22-23 hold the channel count; other formats are treated as mono."""
    if audio.media_format != "wav":
        return 1
    header = _s3().get_object(Bucket=bucket, Key=audio.key, Range="bytes=0-63")["Body"].read()
    return int.from_bytes(header[22:24], "little") if header[:4] == b"RIFF" and len(header) >= 24 else 1


def start_medical_job(bucket: str, audio, channels: int) -> None:
    settings = {"ChannelIdentification": True} if channels == 2 else {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 2}
    _transcribe().start_medical_transcription_job(
        MedicalTranscriptionJobName=job_name(os.environ["JOB_PREFIX"], audio.draft_id),
        LanguageCode="en-US",
        Media={"MediaFileUri": f"s3://{bucket}/{audio.key}"},
        MediaFormat=audio.media_format,
        OutputBucketName=bucket,
        OutputKey=transcript_key(audio.draft_id),
        Specialty="PRIMARYCARE",
        Type="CONVERSATION",
        Settings=settings,
    )


def start_scribe(bucket: str, audio, item: dict, channels: int, mode: str = "pipeline", run: int = 0,
                 template: str | None = None) -> str:
    """Start a HealthScribe job and its watcher. Returns the job name."""
    template = template or item.get("noteTemplate") or "HISTORY_AND_PHYSICAL"
    name = scribe.job_name(os.environ["JOB_PREFIX"], audio.draft_id, template, run)
    try:
        scribe.start_job(_transcribe(), name=name, bucket=bucket, media_uri=f"s3://{bucket}/{audio.key}",
                         role_arn=os.environ["SCRIBE_ROLE_ARN"], template=template, channels=channels,
                         pronouns=item.get("pronouns"))
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConflictException":
            raise
        log("scribe_job_already_started", draftId=audio.draft_id)
    _lambda().invoke(FunctionName=os.environ["WATCHER_FUNCTION"], InvocationType="Event", Payload=json.dumps(
        {"draftId": audio.draft_id, "jobName": name, "template": template, "mode": mode}).encode())
    return name


def handle_record(record: dict) -> str:
    bucket = record["s3"]["bucket"]["name"]
    audio = parse_audio_key(object_key(record))
    if audio is None:
        log("ignored_key", reason="not an audio/{patientId}/{encounterId}/file key")
        return "ignored"

    draft_id = audio.draft_id
    existing = _table().get(draft_id) or {}
    engines = ENGINES.get(existing.get("engine"), ["transcribe"])
    channels = channel_count(bucket, audio)
    item = _table().transition(
        draft_id,
        dt.TRANSCRIBING,
        allowed_from=(dt.UPLOADED, dt.TRANSCRIBING),
        allow_missing=True,
        attrs={
            "patientId": audio.patient_id,
            "encounterId": audio.encounter_id,
            "audioKey": audio.key,
            "jobName": job_name(os.environ["JOB_PREFIX"], draft_id),
            "engines": engines,
            "pendingEngines": set(engines),
            "channels": channels,
        },
    )
    if item is None:
        log("duplicate_ignored", draftId=draft_id)
        return "duplicate"

    outcome = "started"
    for engine in engines:
        try:
            if engine == "transcribe":
                start_medical_job(bucket, audio, channels)
            else:
                start_scribe(bucket, audio, item, channels)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ConflictException":
                log("job_already_started", draftId=draft_id, engine=engine)
                outcome = "already_started"
                continue
            if code in RETRYABLE:
                log("transcribe_retry", draftId=draft_id, engine=engine, errorCode=code)
                raise  # Lambda async retry; status stays TRANSCRIBING, which allows a retry
            prefix = "TRANSCRIBE" if engine == "transcribe" else "SCRIBE"
            _table().fail(draft_id, f"{prefix}_{code}")
            log("transcribe_failed", draftId=draft_id, engine=engine, errorCode=code)
            return "failed"

    log("transcription_started", draftId=draft_id, engines=engines, channels=channels,
        mediaFormat=audio.media_format)
    return outcome


def handler(event, _context):
    return [handle_record(r) for r in event.get("Records", [])]
