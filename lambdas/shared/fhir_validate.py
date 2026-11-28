"""Small structural checks run before a draft is saved and again before writing.

Not a full FHIR validator: HealthLake's own (strict) validation still applies.
"""

import base64
import binascii
import re

from shared.fhir_mapper import AI_ASSERTED

_REF = {
    "Patient": re.compile(r"^Patient/[A-Za-z0-9\-.]{1,64}$"),
    "Encounter": re.compile(r"^Encounter/[A-Za-z0-9\-.]{1,64}$"),
}
_ALLOWED_TYPES = {"DocumentReference", "Condition", "MedicationStatement", "Provenance", "Encounter"}
_MED_STATUSES = {"active", "completed", "entered-in-error", "intended", "stopped", "on-hold", "unknown", "not-taken"}
_VER_STATUSES = {"unconfirmed", "provisional", "differential", "confirmed", "refuted", "entered-in-error"}


def _ref_ok(ref: dict | None, kind: str) -> bool:
    return isinstance(ref, dict) and bool(_REF[kind].match(ref.get("reference", "")))


def _coding_ok(concept: dict | None, system: str | None = None) -> bool:
    codings = (concept or {}).get("coding") or []
    return bool(codings) and all(
        c.get("code") and c.get("system") and (system is None or c["system"] == system) for c in codings
    )


def _no_empty_values(value, path="") -> list[str]:
    """FHIR forbids empty strings, arrays and objects."""
    if value in ("", [], {}, None):
        return [f"empty value at {path or '/'}"]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _no_empty_values(v, f"{path}/{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _no_empty_values(v, f"{path}/{i}")]
    return []


def _ai_asserted(resource: dict) -> bool:
    return any(s.get("code") == AI_ASSERTED["code"] and s.get("system") == AI_ASSERTED["system"]
               for s in resource.get("meta", {}).get("security", []))


def _clinician_entered(resource: dict) -> bool:
    return any(t.get("code") in ("clinician-entered", "clinician-updated") for t in resource.get("meta", {}).get("tag", []))


def validate(resource: dict) -> list[str]:
    """Return a list of problems (empty when the resource looks structurally valid)."""
    kind = resource.get("resourceType")
    if kind not in _ALLOWED_TYPES:
        return [f"unsupported resourceType {kind!r}"]
    problems = _no_empty_values(resource)

    if kind != "Encounter":
        # Every resource needs the AI-asserted label, except ones a clinician entered by hand.
        if not _clinician_entered(resource) and not _ai_asserted(resource):
            problems.append("missing AIAST security label")
    if kind in {"DocumentReference", "Condition", "MedicationStatement", "Encounter"} and not _ref_ok(
        resource.get("subject"), "Patient"
    ):
        problems.append("subject must reference Patient/{id}")

    if kind == "Condition":
        if not _coding_ok(resource.get("code"), "http://hl7.org/fhir/sid/icd-10-cm"):
            problems.append("code needs an ICD-10-CM coding")
        ver = (resource.get("verificationStatus", {}).get("coding") or [{}])[0].get("code")
        if ver not in _VER_STATUSES:
            problems.append("invalid verificationStatus")
        if "encounter" in resource and not _ref_ok(resource["encounter"], "Encounter"):
            problems.append("encounter must reference Encounter/{id}")

    elif kind == "MedicationStatement":
        if resource.get("status") not in _MED_STATUSES:
            problems.append("invalid status")
        if not _coding_ok(resource.get("medicationCodeableConcept"), "http://www.nlm.nih.gov/research/umls/rxnorm"):
            problems.append("medicationCodeableConcept needs an RxNorm coding")
        if "context" in resource and not _ref_ok(resource["context"], "Encounter"):
            problems.append("context must reference Encounter/{id}")

    elif kind == "DocumentReference":
        if resource.get("status") != "current":
            problems.append("status must be 'current'")
        if not _coding_ok(resource.get("type"), "http://loinc.org"):
            problems.append("type needs a LOINC coding")
        attachments = [c.get("attachment", {}) for c in resource.get("content", [])]
        if not attachments:
            problems.append("content[0].attachment is required")
        for attachment in attachments:
            if attachment.get("contentType") != "text/plain":
                problems.append("attachment.contentType must be text/plain")
            try:
                base64.b64decode(attachment.get("data", ""), validate=True)
            except (binascii.Error, ValueError):
                problems.append("attachment.data must be base64")
        for enc in resource.get("context", {}).get("encounter", []):
            if not _ref_ok(enc, "Encounter"):
                problems.append("context.encounter must reference Encounter/{id}")

    elif kind == "Provenance":
        targets = resource.get("target", [])
        if not targets or not all(re.match(r"^[A-Za-z]+/[A-Za-z0-9\-.]{1,64}(/_history/[0-9]+)?$", t.get("reference", ""))
                                  for t in targets):
            problems.append("target must list resource references")
        if not resource.get("recorded"):
            problems.append("recorded is required")
        roles = {(a.get("type", {}).get("coding") or [{}])[0].get("code") for a in resource.get("agent", [])}
        if "author" not in roles:
            problems.append("agent needs an author")
        if _ai_asserted(resource) and "verifier" not in roles:
            problems.append("AI-authored provenance needs a verifier")
        if not all(a.get("who") for a in resource.get("agent", [])):
            problems.append("agent.who is required")

    elif kind == "Encounter":
        if resource.get("status") not in {"planned", "arrived", "triaged", "in-progress", "onleave", "finished",
                                          "cancelled", "entered-in-error", "unknown"}:
            problems.append("invalid status")
        if not resource.get("class", {}).get("code"):
            problems.append("class is required")

    return problems
