"""Approve a draft: write only the clinician-selected resources to HealthLake.

Order: Encounter (PUT with the draft's encounter id, so references resolve), the selected
resources in draft order (POST), then Provenance (POST): one for AI-derived resources
(author = AI-assisted pipeline, verifier = the Cognito clinician; includes clinician-edited
ones) and, if any were added by hand, one for those (author = the clinician). Every write is recorded in DynamoDB as it happens, so a
retry after a failure resumes and never writes a resource twice (except in the rare case
where the Lambda stops between a write and its record).
"""

import copy
import time

from shared import drafts_table as dt
from shared.fhir_mapper import AI_ASSERTED
from shared.fhir_validate import validate
from shared.healthlake import HealthLakeError

LOCK_SECONDS = 60
MAX_SELECTED = 100
PARTICIPANT = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"


class ApproveError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def build_encounter(item: dict, start: str | None = None) -> dict:
    """start: the consultation date/time the clinician set at review (defaults to when the draft was created)."""
    return {
        "resourceType": "Encounter",
        "id": item["encounterId"],
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB",
                  "display": "ambulatory"},
        "type": [{"text": "Primary care consultation (voice-to-FHIR demo, synthetic)"}],
        "subject": {"reference": f"Patient/{item['patientId']}"},
        "period": {"start": start or item["createdAt"]},
    }


def _agent(role: str, who: dict) -> dict:
    return {"type": {"coding": [{"system": PARTICIPANT, "code": role, "display": role.capitalize()}]}, "who": who}


def build_provenance(targets: list[str], user: dict, draft_id: str, now: str, ai: bool = True,
                     pipeline_label: str | None = None) -> dict:
    """ai=True: AI pipeline authored, clinician verified. ai=False: clinician authored (entered by hand)."""
    clinician = {"display": user.get("name") or "Clinician"}
    if user.get("iss") and user.get("sub"):
        clinician["identifier"] = {"system": user["iss"], "value": user["sub"]}
    pipeline = {"display": pipeline_label or "AI-assisted pipeline (Amazon Transcribe Medical, Amazon Comprehend Medical)"}
    resource = {
        "resourceType": "Provenance",
        "target": [{"reference": ref} for ref in targets],
        "recorded": now,
        "activity": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                                 "code": "CREATE", "display": "create"}]},
        "agent": [_agent("author", pipeline), _agent("verifier", clinician)] if ai else [_agent("author", clinician)],
        "entity": [{"role": "source", "what": {"display": f"Consultation audio, voice-to-FHIR draft {draft_id}"}}],
    }
    if ai:
        resource["meta"] = {"security": [dict(AI_ASSERTED)]}
    else:
        resource["meta"] = {"tag": [{"system": "http://example.org/fhir/CodeSystem/voice-to-fhir-review",
                                     "code": "clinician-entered", "display": "Entered by clinician"}]}
    return resource


def _result(item: dict, order: list[str]) -> dict:
    ids = item.get("healthLakeIds", {})
    written = []
    for key in order:
        if key in ids:
            kind, _, rid = ids[key].partition("/")
            written.append({"proposedId": key, "resourceType": kind, "id": rid})
    return {"draft": item, "written": written}


def approve(*, draft_id: str, requested_ids, user: dict, table: dt.DraftsTable, read_draft, healthlake,
            clock=time.time, feedback: str | None = None, save_review=None, encounter_start: str | None = None) -> dict:
    """save_review(draft, feedback) stores the clinician's optional feedback with the draft (once)."""
    if (not isinstance(requested_ids, list) or not requested_ids or len(requested_ids) > MAX_SELECTED
            or not all(isinstance(i, str) for i in requested_ids)):
        raise ApproveError(400, "BAD_SELECTION", "Select at least one proposed resource.")
    requested = sorted(set(requested_ids))

    item = table.get(draft_id)
    if item is None:
        raise ApproveError(404, "NOT_FOUND", "Draft not found.")
    status = item.get("status")
    if status == dt.WRITTEN:
        if sorted(item.get("approvedIds", [])) == requested:  # idempotent repeat
            draft = read_draft(draft_id)
            return _result(item, _order(draft, requested))
        raise ApproveError(409, "ALREADY_WRITTEN", "This draft was already written with a different selection.")
    if status not in (dt.PENDING_REVIEW, dt.APPROVED):
        raise ApproveError(409, "NOT_PENDING_REVIEW", f"Draft is {status}; it cannot be approved.")

    draft = read_draft(draft_id)
    proposed = {p["id"]: p for p in draft["proposed"]}
    unknown = [i for i in requested if i not in proposed]
    if unknown:
        raise ApproveError(400, "UNKNOWN_RESOURCE_IDS", "Some selected resources are not in this draft.",
                           ids=unknown)
    order = _order(draft, requested)
    encounter = build_encounter(item, encounter_start)
    invalid = [key for key, res in [("encounter", encounter), *((i, proposed[i]["resource"]) for i in requested)]
               if validate(res)]
    if invalid:
        raise ApproveError(422, "VALIDATION_FAILED", "Some resources failed validation.", ids=invalid)

    now = int(clock())
    if status == dt.PENDING_REVIEW:
        locked = table.start_approval(draft_id, requested, user["sub"], now, LOCK_SECONDS)
        if locked is None:
            raise ApproveError(409, "CONFLICT", "The draft changed; reload and try again.")
    else:
        if sorted(item.get("approvedIds", [])) != requested:
            raise ApproveError(409, "APPROVAL_MISMATCH", "Retry with the same selection as the first approval.")
        locked = table.acquire_write_lock(draft_id, now, LOCK_SECONDS)
        if locked is None:
            raise ApproveError(409, "WRITE_IN_PROGRESS", "This draft is being written; wait and refresh.")

    written = dict(locked.get("healthLakeIds", {}))
    if save_review is not None and draft.get("review") is None:
        try:
            save_review(draft, feedback)
        except Exception:
            table.release_write_lock(draft_id, "FEEDBACK_NOT_SAVED")
            raise ApproveError(502, "FEEDBACK_NOT_SAVED", "Could not save your feedback. Approve again to retry.",
                               writtenCount=len(written)) from None

    def write(key, fn, resource):
        if key not in written:
            written[key] = fn(resource)
            table.record_written(draft_id, key, written[key])

    try:
        write("encounter", healthlake.put, encounter)
        for pid in requested_in_draft_order(draft, requested):
            write(pid, healthlake.create, copy.deepcopy(proposed[pid]["resource"]))
        selected = requested_in_draft_order(draft, requested)
        groups = (("provenance", True, [p for p in selected if proposed[p].get("origin") != "clinician"]),
                  ("provenance-clinician", False, [p for p in selected if proposed[p].get("origin") == "clinician"]))
        for key, ai, pids in groups:
            if not pids:
                continue
            provenance = build_provenance([written[p] for p in pids], user, draft_id, dt.utc_now(), ai=ai,
                                          pipeline_label=draft.get("aiPipeline"))
            if validate(provenance):
                raise ApproveError(422, "VALIDATION_FAILED", "Provenance failed validation.", ids=[key])
            write(key, healthlake.create, provenance)
    except HealthLakeError as exc:
        if exc.retryable:
            table.release_write_lock(draft_id, f"HEALTHLAKE_{exc.status or 'NETWORK'}")
            raise ApproveError(502, "HEALTHLAKE_WRITE_FAILED",
                               "Writing to HealthLake failed part-way. Approve again to resume.",
                               writtenCount=len(written)) from None
        table.fail(draft_id, f"HEALTHLAKE_REJECTED_{exc.status}")
        raise ApproveError(502, "HEALTHLAKE_REJECTED", "HealthLake rejected a resource; the draft is marked failed.",
                           writtenCount=len(written), issues=exc.issue_codes) from None

    final = table.finish_written(draft_id) or table.get(draft_id)
    return _result(final, order)


def requested_in_draft_order(draft: dict, requested: list[str]) -> list[str]:
    wanted = set(requested)
    return [p["id"] for p in draft["proposed"] if p["id"] in wanted]


def _order(draft: dict, requested: list[str]) -> list[str]:
    return ["encounter", *requested_in_draft_order(draft, requested), "provenance", "provenance-clinician"]
