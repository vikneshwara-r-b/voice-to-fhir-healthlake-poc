"""Per-engine results and the final draft assembly.

A draft runs on one engine ("transcribe" or "healthscribe") or both. Each engine writes
results/{draftId}/{engine}.json and removes itself from the draft's pendingEngines set (an
atomic DynamoDB update); the engine that empties the set assembles the draft. With both,
HealthScribe is the primary engine (proposed FHIR comes from its transcript, roles and note)
and the Transcribe Medical result is kept for the side-by-side comparison.
"""

import json
from urllib.parse import unquote, urlparse

from shared import drafts_table as dt
from shared.fhir_mapper import build_draft
from shared.fhir_validate import validate
from shared.keys import draft_key, nlp_key
from shared.log import log
from shared.scribe import ENGINE_LABELS

ENGINES = ("transcribe", "healthscribe")
TRANSCRIPT_SOURCE = {"transcribe": "Amazon Transcribe Medical", "healthscribe": "AWS HealthScribe"}
ROLE_METHOD = {"speaker-order": "First speaker labelled Clinician (Transcribe Medical speaker labels)",
               "channel": "Audio channel: left = Clinician, right = Patient (channel identification)",
               "healthscribe": "Participant roles identified by AWS HealthScribe"}


class EngineError(Exception):
    """Carries a short, PHI-free error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def result_key(draft_id: str, engine: str) -> str:
    return f"results/{draft_id}/{engine}.json"


def note_key(draft_id: str, template: str) -> str:
    return f"notes/{draft_id}/{template}.json"


def put_json(s3, bucket: str, key: str, body: dict) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(body).encode("utf-8"), ContentType="application/json")


def get_json(s3, bucket: str, key: str) -> dict:
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """s3://bucket/key, https://s3.<region>.amazonaws.com/bucket/key or https://bucket.s3.<region>.amazonaws.com/key."""
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        return parsed.netloc, unquote(parsed.path.lstrip("/"))
    host, path = parsed.netloc, unquote(parsed.path.lstrip("/"))
    if host.startswith("s3.") or host.startswith("s3-"):
        bucket, _, key = path.partition("/")
        return bucket, key
    return host.split(".s3", 1)[0], path


def _proposals(result: dict, item: dict, thresholds: dict) -> list[dict]:
    draft = build_draft(draft_id=item["draftId"], patient_id=item["patientId"], encounter_id=item["encounterId"],
                        turns=result["turns"], nlp=result["nlp"], now=dt.utc_now(), **thresholds)
    return [{"resourceType": p["resourceType"], "code": p["code"], "label": p["label"], "flagged": p["flagged"]}
            for p in draft["proposed"] if p["resourceType"] in ("Condition", "MedicationStatement")]


def assemble(*, s3, bucket: str, item: dict, thresholds: dict) -> dict:
    draft_id = item["draftId"]
    engines = [e for e in ENGINES if e in item.get("engines", ["transcribe"])]
    results = {e: get_json(s3, bucket, result_key(draft_id, e)) for e in engines}
    primary = "healthscribe" if "healthscribe" in results else "transcribe"
    main = results[primary]

    draft = build_draft(draft_id=draft_id, patient_id=item["patientId"], encounter_id=item["encounterId"],
                        turns=main["turns"], nlp=main["nlp"], now=dt.utc_now(), note=main.get("note"),
                        transcript_source=TRANSCRIPT_SOURCE[primary], **thresholds)
    invalid = [p["id"] for p in draft["proposed"] if validate(p["resource"])]
    if invalid:
        log("draft_validation_failed", draftId=draft_id, invalidIds=invalid)
        raise EngineError("DRAFT_VALIDATION_FAILED")

    draft.update(primaryEngine=primary, aiPipeline=f"AI-assisted pipeline ({ENGINE_LABELS[primary]})",
                 roleMethod=ROLE_METHOD.get(main.get("roleMethod"), main.get("roleMethod")),
                 noteTemplate=item.get("noteTemplate") if "healthscribe" in results else None)
    if len(results) > 1:
        draft["comparison"] = {
            engine: {
                "label": ENGINE_LABELS[engine],
                "roleMethod": ROLE_METHOD.get(r.get("roleMethod"), r.get("roleMethod")),
                "turns": [{"role": t["role"], "text": t["text"]} for t in r["turns"]],
                "wordCount": sum(len(t["text"].split()) for t in r["turns"]),
                "proposals": _proposals(r, item, thresholds),
                "insights": r.get("insights"),
            }
            for engine, r in results.items()
        }
    put_json(s3, bucket, nlp_key(draft_id), {"draftId": draft_id, "engine": primary, **main["nlp"]})
    put_json(s3, bucket, draft_key(draft_id), draft)
    return {
        "turnCount": len(main["turns"]),
        "chunkCount": main["nlp"].get("chunkCount", 0),
        "entityCount": len(main["nlp"]["entities"]),
        "proposedCount": len(draft["proposed"]),
        "flaggedCount": sum(1 for p in draft["proposed"] if p["flagged"]),
        "excludedCount": len(draft["excluded"]),
    }


def finish_engine(*, s3, bucket: str, table: dt.DraftsTable, draft_id: str, engine: str, result: dict,
                  thresholds: dict) -> str:
    put_json(s3, bucket, result_key(draft_id, engine), result)
    item = table.complete_engine(draft_id, engine)
    if item is None:
        log("engine_already_completed", draftId=draft_id, engine=engine)
        return "duplicate"
    if item.get("pendingEngines"):
        log("engine_done_waiting", draftId=draft_id, engine=engine, waitingFor=sorted(item["pendingEngines"]))
        return "waiting"
    counts = assemble(s3=s3, bucket=bucket, item=item, thresholds=thresholds)
    table.transition(draft_id, dt.PENDING_REVIEW, allowed_from=(dt.TRANSCRIBING, dt.ANALYZING), attrs=counts)
    log("draft_ready", draftId=draft_id, engine=engine, **counts)
    return "ready"
