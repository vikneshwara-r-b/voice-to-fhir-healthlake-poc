"""Encounter date/time: parse clinician input and update an Encounter in HealthLake with Provenance."""

from datetime import datetime, timedelta, timezone

from shared.fhir_validate import validate
from shared.healthlake import HealthLakeError

PARTICIPANT = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
UPDATED_TAG = {"system": "http://example.org/fhir/CodeSystem/voice-to-fhir-review", "code": "clinician-updated",
               "display": "Updated by clinician"}
KEEP_META = ("security", "tag", "profile")  # versionId/lastUpdated are set by HealthLake


class EncounterError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def parse_instant(value, field: str, required: bool = True, now: datetime | None = None) -> str | None:
    """ISO 8601 with a timezone (the browser sends UTC), between 1900 and one day from now."""
    if value in (None, ""):
        if required:
            raise EncounterError(400, "MISSING_FIELD", f"'{field}' is required.")
        return None
    if not isinstance(value, str):
        raise EncounterError(400, "BAD_DATETIME", f"'{field}' must be a date and time.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EncounterError(400, "BAD_DATETIME", f"'{field}' is not a valid date and time.") from None
    if parsed.tzinfo is None:
        raise EncounterError(400, "BAD_DATETIME", f"'{field}' needs a timezone.")
    now = now or datetime.now(timezone.utc)
    if not datetime(1900, 1, 1, tzinfo=timezone.utc) <= parsed <= now + timedelta(days=1):
        raise EncounterError(400, "DATETIME_OUT_OF_RANGE", f"'{field}' must be between 1900 and today.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def update_period(*, healthlake, patient_id: str, encounter_id: str, start: str, end: str | None,
                  user: dict, now: str) -> dict:
    try:
        encounter = healthlake.read("Encounter", encounter_id)
    except HealthLakeError as exc:
        if exc.status == 404:
            raise EncounterError(404, "NOT_FOUND", "Encounter not found.") from None
        raise
    if encounter.get("subject", {}).get("reference") != f"Patient/{patient_id}":
        raise EncounterError(404, "NOT_FOUND", "Encounter not found for this patient.")
    if end and end < start:
        raise EncounterError(400, "END_BEFORE_START", "The end must be after the start.")

    meta = {k: v for k, v in encounter.get("meta", {}).items() if k in KEEP_META}
    tags = meta.setdefault("tag", [])
    if not any(t.get("code") == UPDATED_TAG["code"] for t in tags):
        tags.append(dict(UPDATED_TAG))
    encounter["meta"] = meta
    encounter["period"] = {"start": start, **({"end": end} if end else {})}
    problems = validate(encounter)
    if problems:
        raise EncounterError(422, "VALIDATION_FAILED", "The updated encounter is not valid: " + "; ".join(problems))

    healthlake.put(encounter)
    version = healthlake.read("Encounter", encounter_id).get("meta", {}).get("versionId")
    target = f"Encounter/{encounter_id}" + (f"/_history/{version}" if version else "")
    who = {"display": user.get("name") or "Clinician"}
    if user.get("iss") and user.get("sub"):
        who["identifier"] = {"system": user["iss"], "value": user["sub"]}
    provenance = {
        "resourceType": "Provenance",
        "meta": {"tag": [dict(UPDATED_TAG)]},
        "target": [{"reference": target}],
        "recorded": now,
        "activity": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                                 "code": "UPDATE", "display": "revise"}]},
        "agent": [{"type": {"coding": [{"system": PARTICIPANT, "code": "author", "display": "Author"}]}, "who": who}],
        "reason": [{"text": "Encounter date and time corrected by clinician"}],
    }
    problems = validate(provenance)
    if problems:
        raise EncounterError(422, "VALIDATION_FAILED", "Provenance is not valid: " + "; ".join(problems))
    provenance_ref = healthlake.create(provenance)
    return {"encounter": encounter, "versionId": version, "provenance": provenance_ref}
