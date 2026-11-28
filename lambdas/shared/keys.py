"""S3 key layout. The draft id is the encounter id (one audio file per encounter).

audio/{patientId}/{encounterId}/{name}.{ext}  -> start_transcription
transcripts/{draftId}.json                    -> analyze_transcript (written by Transcribe)
nlp/{draftId}.json, drafts/{draftId}.json      written by analyze_transcript
"""

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus

AUDIO_EXTENSIONS = ("wav", "mp3", "flac", "m4a")
FHIR_ID = r"[A-Za-z0-9][A-Za-z0-9\-.]{0,63}"  # FHIR id rules, but no leading "." or "-"
DRAFT_ID = r"[A-Za-z0-9][A-Za-z0-9\-]{0,63}"

_AUDIO_KEY = re.compile(
    rf"^audio/(?P<patient_id>{FHIR_ID})/(?P<encounter_id>{DRAFT_ID})/"
    rf"[A-Za-z0-9_\-.]{{1,100}}\.(?P<ext>{'|'.join(AUDIO_EXTENSIONS)})$"
)
_TRANSCRIPT_KEY = re.compile(rf"^transcripts/(?P<draft_id>{DRAFT_ID})\.json$")
_DRAFT_ID_ONLY = re.compile(rf"^{DRAFT_ID}$")
_FHIR_ID_ONLY = re.compile(rf"^{FHIR_ID}$")


@dataclass(frozen=True)
class AudioKey:
    key: str
    patient_id: str
    encounter_id: str
    media_format: str

    @property
    def draft_id(self) -> str:
        return self.encounter_id


def object_key(record: dict) -> str:
    """S3 event keys are URL-encoded (spaces as '+')."""
    return unquote_plus(record["s3"]["object"]["key"])


def parse_audio_key(key: str) -> AudioKey | None:
    m = _AUDIO_KEY.match(key)
    if not m:
        return None
    return AudioKey(key, m["patient_id"], m["encounter_id"], m["ext"])


def parse_transcript_key(key: str) -> str | None:
    m = _TRANSCRIPT_KEY.match(key)
    return m["draft_id"] if m else None


def is_draft_id(value: str) -> bool:
    return bool(_DRAFT_ID_ONLY.match(value or ""))


def is_fhir_id(value: str) -> bool:
    return bool(_FHIR_ID_ONLY.match(value or ""))


def job_name(prefix: str, draft_id: str) -> str:
    return f"{prefix}-{draft_id}"


def transcript_key(draft_id: str) -> str:
    return f"transcripts/{draft_id}.json"


def nlp_key(draft_id: str) -> str:
    return f"nlp/{draft_id}.json"


def draft_key(draft_id: str) -> str:
    return f"drafts/{draft_id}.json"
