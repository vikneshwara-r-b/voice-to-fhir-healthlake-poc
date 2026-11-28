"""Clinician edits to a draft: edit a proposed resource, add one, record review feedback.

The browser sends a few named fields, never raw FHIR. This module builds the resource,
keeps the original AI version for audit, tags the change, and validates the result.
Edited AI items keep the AIAST label and gain a "clinician-edited" tag; added items
carry a "clinician-entered" tag and no AIAST label.
"""

import base64
import copy
import re

from shared.fhir_mapper import ICD10CM_SYSTEM, MAX_NOTE_CHARS, RXNORM_SYSTEM
from shared.fhir_validate import validate

REVIEW_TAG_SYSTEM = "http://example.org/fhir/CodeSystem/voice-to-fhir-review"
EDITED_TAG = {"system": REVIEW_TAG_SYSTEM, "code": "clinician-edited", "display": "Edited by clinician"}
ENTERED_TAG = {"system": REVIEW_TAG_SYSTEM, "code": "clinician-entered", "display": "Entered by clinician"}

CLINICAL = {"active", "recurrence", "relapse", "inactive", "remission", "resolved"}
VERIFICATION = {"unconfirmed", "provisional", "differential", "confirmed", "refuted"}
MED_STATUS = {"active", "completed", "intended", "stopped", "on-hold", "not-taken"}
ICD10CM_CODE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
RXNORM_CODE = re.compile(r"^[0-9]{1,10}$")
ITEM_ID = re.compile(r"^[a-z]+-[0-9]{1,3}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_FEEDBACK = 2000
MIN_FEEDBACK = 5


class EditError(Exception):
    def __init__(self, status: int, code: str, message: str, problems: list[str] | None = None):
        super().__init__(code)
        self.status, self.code, self.message, self.problems = status, code, message, problems or []


def _text(fields: dict, name: str, max_len: int, required: bool = True) -> str:
    value = fields.get(name, "")
    if not isinstance(value, str):
        raise EditError(400, "BAD_FIELD", f"'{name}' must be text.")
    value = value.strip()
    if _CONTROL.search(value):
        raise EditError(400, "BAD_FIELD", f"'{name}' contains control characters.")
    if required and not value:
        raise EditError(400, "MISSING_FIELD", f"'{name}' is required.")
    if len(value) > max_len:
        raise EditError(400, "FIELD_TOO_LONG", f"'{name}' is longer than {max_len} characters.")
    return value


def _choice(fields: dict, name: str, allowed: set[str]) -> str:
    value = fields.get(name)
    if value not in allowed:
        raise EditError(400, "BAD_FIELD", f"'{name}' must be one of: {', '.join(sorted(allowed))}.")
    return value


def _coding_fields(fields: dict, kind: str) -> tuple[str, str]:
    code = _text(fields, "code", 12).upper()
    pattern = ICD10CM_CODE if kind == "Condition" else RXNORM_CODE
    if not pattern.match(code):
        hint = "an ICD-10-CM code such as I10 or M25.561" if kind == "Condition" else "a numeric RxNorm code"
        raise EditError(400, "BAD_CODE", f"Enter {hint}.")
    return code, _text(fields, "display", 200)


def _status_coding(system_suffix: str, code: str) -> dict:
    return {"coding": [{"system": f"http://terminology.hl7.org/CodeSystem/{system_suffix}", "code": code}]}


def _apply_condition(resource: dict, fields: dict) -> tuple[str, str]:
    code, display = _coding_fields(fields, "Condition")
    resource["code"] = {"coding": [{"system": ICD10CM_SYSTEM, "code": code, "display": display}], "text": display}
    resource["clinicalStatus"] = _status_coding("condition-clinical", _choice(fields, "clinicalStatus", CLINICAL))
    resource["verificationStatus"] = _status_coding("condition-ver-status",
                                                    _choice(fields, "verificationStatus", VERIFICATION))
    return code, display


def _apply_medication(resource: dict, fields: dict) -> tuple[str, str]:
    code, display = _coding_fields(fields, "MedicationStatement")
    resource["medicationCodeableConcept"] = {
        "coding": [{"system": RXNORM_SYSTEM, "code": code, "display": display}], "text": display}
    resource["status"] = _choice(fields, "status", MED_STATUS)
    dosage = _text(fields, "dosage", 200, required=False)
    if dosage:
        resource["dosage"] = [{"text": dosage}]
    else:
        resource.pop("dosage", None)
    return code, display


def _apply_transcript(resource: dict, fields: dict) -> None:
    text = fields.get("transcript", "")
    if not isinstance(text, str) or not text.strip():
        raise EditError(400, "MISSING_FIELD", "'transcript' is required.")
    if len(text) > MAX_NOTE_CHARS:
        raise EditError(400, "FIELD_TOO_LONG", f"The transcript is longer than {MAX_NOTE_CHARS} characters.")
    resource["content"][0]["attachment"]["data"] = base64.b64encode(text.strip().encode("utf-8")).decode("ascii")


def _tag(resource: dict, tag: dict) -> None:
    tags = resource.setdefault("meta", {}).setdefault("tag", [])
    if not any(t.get("code") == tag["code"] and t.get("system") == tag["system"] for t in tags):
        tags.append(dict(tag))


def _check(resource: dict) -> None:
    problems = validate(resource)
    if problems:
        raise EditError(422, "VALIDATION_FAILED", "The resource is not valid FHIR.", problems)


def edit_item(draft: dict, item_id: str, fields: dict) -> dict:
    if not ITEM_ID.match(item_id or ""):
        raise EditError(400, "BAD_ITEM_ID", "Invalid item id.")
    item = next((p for p in draft["proposed"] if p["id"] == item_id), None)
    if item is None:
        raise EditError(404, "ITEM_NOT_FOUND", "That resource is not in this draft.")
    resource = copy.deepcopy(item["resource"])
    kind = item["resourceType"]
    if kind == "Condition":
        item_code, label = _apply_condition(resource, fields)
    elif kind == "MedicationStatement":
        item_code, label = _apply_medication(resource, fields)
    elif kind == "DocumentReference":
        _apply_transcript(resource, fields)
        item_code, label = item["code"], item["label"]
    else:
        raise EditError(400, "NOT_EDITABLE", f"{kind} cannot be edited.")
    if item.get("origin") != "clinician":
        _tag(resource, EDITED_TAG)
        item.setdefault("aiResource", item["resource"])  # the original AI suggestion, kept for audit
    _check(resource)

    item.update(resource=resource, code=item_code, label=label, edited=True)
    if kind == "DocumentReference":
        item["flagReasons"] = [r for r in item["flagReasons"] if r != "TRANSCRIPT_OVER_10000_CHARS"]
        item["flagged"] = bool(item["flagReasons"])
    return item


def add_item(draft: dict, fields: dict, now: str) -> dict:
    kind = fields.get("resourceType")
    if kind not in ("Condition", "MedicationStatement"):
        raise EditError(400, "BAD_RESOURCE_TYPE", "You can add a Condition or a MedicationStatement.")
    subject = {"reference": f"Patient/{draft['patientId']}"}
    encounter = {"reference": f"Encounter/{draft['encounterId']}"}
    if kind == "Condition":
        resource = {
            "resourceType": "Condition",
            "meta": {"tag": [dict(ENTERED_TAG)]},
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                      "code": "encounter-diagnosis", "display": "Encounter Diagnosis"}]}],
            "subject": subject, "encounter": encounter, "recordedDate": now,
        }
        code, label = _apply_condition(resource, fields)
    else:
        resource = {"resourceType": "MedicationStatement", "meta": {"tag": [dict(ENTERED_TAG)]},
                    "subject": subject, "context": encounter, "dateAsserted": now}
        code, label = _apply_medication(resource, fields)
    _check(resource)

    prefix = kind.lower()
    used = [int(p["id"].rsplit("-", 1)[1]) for p in draft["proposed"] if p["id"].startswith(prefix + "-")]
    item = {
        "id": f"{prefix}-{max(used, default=0) + 1}", "resourceType": kind, "code": code, "label": label,
        "sourceText": None, "turnIndex": None, "flagged": False, "flagReasons": [], "selected": True,
        "confidence": None, "codeScore": None, "origin": "clinician", "edited": False, "resource": resource,
    }
    draft["proposed"].append(item)
    return item


def parse_feedback(body: dict, required: bool) -> str | None:
    value = body.get("feedback")
    if value is None or value == "":
        if required:
            raise EditError(400, "FEEDBACK_REQUIRED", "Feedback is required to reject a draft.")
        return None
    if not isinstance(value, str):
        raise EditError(400, "BAD_FIELD", "Feedback must be text.")
    value = _CONTROL.sub("", value).strip()
    if required and len(value) < MIN_FEEDBACK:
        raise EditError(400, "FEEDBACK_REQUIRED", f"Feedback needs at least {MIN_FEEDBACK} characters.")
    if len(value) > MAX_FEEDBACK:
        raise EditError(400, "FIELD_TOO_LONG", f"Feedback is longer than {MAX_FEEDBACK} characters.")
    return value or None


def set_review(draft: dict, decision: str, feedback: str | None, user_sub: str, now: str) -> None:
    draft["review"] = {"decision": decision, "feedback": feedback, "reviewedBy": user_sub, "reviewedAt": now}
