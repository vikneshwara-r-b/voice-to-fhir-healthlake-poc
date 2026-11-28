#!/usr/bin/env python3
"""Local mock of Cognito + the voice-to-FHIR API, serving the real frontend. Never calls AWS.

For UI smoke tests and offline demos. Draft data is built with the real server-side code
(parsers, Comprehend Medical mapping, HealthScribe note parsing) from synthetic inputs.

    python3 tests/ui/mock_api.py [--port 8765] [--overlay DIR]
    open http://127.0.0.1:8765/   (any email, any 8+ character password)

--overlay DIR: files in DIR are served before frontend/ (e.g. scripted smoke-test pages).
"""

import argparse
import base64
import json
import sys
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "lambdas"), str(ROOT / "scripts"), str(ROOT / "tests" / "unit")]

import sample_fhir_data as sfd  # noqa: E402
import test_scribe as ts  # noqa: E402
from conftest import cm_entity  # noqa: E402
from datetime import date  # noqa: E402
from shared import scribe as sc  # noqa: E402
from shared.chart import build_chart  # noqa: E402
from shared.draft_edit import EditError, add_item, edit_item  # noqa: E402
from shared.engines import ROLE_METHOD  # noqa: E402
from shared.fhir_mapper import build_draft, note_reference  # noqa: E402
from shared.nlp import analyze  # noqa: E402
from shared.review import entity_rows  # noqa: E402

API, COGNITO = "/mock/api", "/mock/cognito"
CONFIG_JS = (f"export const CONFIG = Object.freeze({{region: 'us-east-1', userPoolId: 'local', "
             f"userPoolClientId: 'local-client', cognitoEndpoint: '{COGNITO}', apiUrl: '{API}'}});\n")
THRESHOLDS = {"confidence": 0.8, "codeScoreFloor": 0.3}

# --- synthetic data --------------------------------------------------------------------------
ELENA = sfd.PATIENTS[0]
PID = ELENA["id"]
_records: dict[str, list] = {}
for r in sfd.all_resources(ELENA)[1:]:
    _records.setdefault(r["resourceType"], []).append(r)
CHART = build_chart(sfd.patient_resource(ELENA), _records, date(2026, 11, 27))
PATIENTS = [CHART["patient"], *[build_chart(sfd.patient_resource(p), {}, date(2026, 11, 27))["patient"]
                                for p in sfd.PATIENTS[1:]]]

# d-1: Transcribe Medical draft
_turns = [{"index": 0, "speaker": "spk_0", "role": "Clinician", "confidence": 0.98,
           "text": "Any chest pain? Your hypertension is not well controlled."},
          {"index": 1, "speaker": "spk_1", "role": "Patient", "confidence": 0.97,
           "text": "No chest pain. My father had a heart attack."}]
_nlp = {"icd10cm": [cm_entity("hypertension", "MEDICAL_CONDITION", traits=["DIAGNOSIS"], score=0.93, begin=21,
                              codes=[("I10", "Essential (primary) hypertension", 0.64)]),
                    cm_entity("chest pain", "MEDICAL_CONDITION", traits=["SYMPTOM", "NEGATION"], begin=4,
                              codes=[("R07.9", "Chest pain, unspecified", 0.6)])],
        "rxnorm": [cm_entity("amlodipine", "MEDICATION", score=0.6, codes=[("197361", "amlodipine 5 MG", 0.92)])],
        "entities": []}
DRAFTS = {"d-1": {"item": {"draftId": "d-1", "patientId": PID, "status": "PENDING_REVIEW", "engine": "transcribe",
                           "createdAt": "2026-11-27T09:00:00Z"},
                  "draft": build_draft(draft_id="d-1", patient_id=PID, encounter_id="d-1", turns=_turns, nlp=_nlp,
                                       now="2026-11-27T09:00:00Z"),
                  "nlp": _nlp, "notes": {}, "extra": {"primaryEngine": "transcribe",
                                                      "roleMethod": ROLE_METHOD["speaker-order"]}}}

# d-2: both engines, HealthScribe primary
_t, _segs, _ins = sc.parse_transcript(ts.scribe_transcript())
_note = sc.parse_note(ts.scribe_note(), "HISTORY_AND_PHYSICAL", _segs)
_nlp2 = analyze(_t, ts.FakeComprehendMedical())
DRAFTS["d-2"] = {
    "item": {"draftId": "d-2", "patientId": PID, "status": "PENDING_REVIEW", "engine": "both",
             "engines": ["transcribe", "healthscribe"], "createdAt": "2026-11-27T10:00:00Z"},
    "draft": build_draft(draft_id="d-2", patient_id=PID, encounter_id="d-2", turns=_t, nlp=_nlp2,
                         now="2026-11-27T10:00:00Z", note=_note, transcript_source="AWS HealthScribe"),
    "nlp": _nlp2, "notes": {"HISTORY_AND_PHYSICAL": _note},
    "extra": {"primaryEngine": "healthscribe", "roleMethod": ROLE_METHOD["healthscribe"],
              "noteTemplate": "HISTORY_AND_PHYSICAL", "comparison": {
                  "transcribe": {"label": sc.ENGINE_LABELS["transcribe"], "roleMethod": ROLE_METHOD["speaker-order"],
                                 "turns": [{"role": "Clinician", "text": "Any chest pain?"}], "wordCount": 3,
                                 "proposals": [], "insights": None},
                  "healthscribe": {"label": sc.ENGINE_LABELS["healthscribe"], "roleMethod": ROLE_METHOD["healthscribe"],
                                   "turns": [{"role": t["role"], "text": t["text"]} for t in _t], "wordCount": 9,
                                   "proposals": [{"resourceType": "Condition", "code": "I10", "flagged": False,
                                                  "label": "Essential (primary) hypertension"}],
                                   "insights": _ins}}},
}
PENDING_NOTES: dict[str, dict] = {}  # draftId -> {template: polls}


def draft_response(draft_id: str) -> dict:
    d = DRAFTS[draft_id]
    for template, polls in list(PENDING_NOTES.get(draft_id, {}).items()):  # "finish" after one poll
        if polls >= 1:
            d["notes"][template] = dict(_note, template=template, templateTitle=sc.TEMPLATES[template][0])
            del PENDING_NOTES[draft_id][template]
        else:
            PENDING_NOTES[draft_id][template] += 1
    item = dict(d["item"], pendingNotes=sorted(PENDING_NOTES.get(draft_id, {})))
    return {"draft": item, "turns": d["draft"]["turns"], "proposed": d["draft"]["proposed"],
            "excluded": d["draft"]["excluded"], "entities": entity_rows(d["nlp"]), "thresholds": THRESHOLDS,
            "notes": d["notes"], "review": None, **d["extra"]}


def fake_jwt(email: str) -> str:
    b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")  # noqa: E731
    claims = {"email": email, "name": "John Smith", "cognito:username": "00000000-mock-user-0000-000000000000",
              "exp": int(time.time()) + 3600}
    return f"{b64({'alg': 'none'})}.{b64(claims)}.mock"


class Handler(SimpleHTTPRequestHandler):
    overlay: Path | None = None

    def log_message(self, *args):
        pass

    def _json(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def translate_path(self, path):
        if self.overlay and (self.overlay / path.lstrip("/").split("?")[0]).is_file():
            return str(self.overlay / path.lstrip("/").split("?")[0])
        return super().translate_path(path)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/js/config.js":
            data = CONFIG_JS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.end_headers()
            return self.wfile.write(data)
        if not path.startswith(API):
            return super().do_GET()
        route = path[len(API):]
        if route == "/patients":
            return self._json(200, {"patients": PATIENTS})
        if route == f"/patients/{PID}/summary":
            return self._json(200, CHART)
        if route == f"/patients/{PID}/fhir":
            kind = query.split("type=")[-1] or "Patient"
            body = sfd.patient_resource(ELENA) if kind == "Patient" else {
                "resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in _records.get(kind, [])]}
            return self._json(200, {"request": {"method": "GET", "signedWith": "AWS SigV4 (service healthlake)",
                                                "url": f"https://healthlake.us-east-1.amazonaws.com/datastore/mock/r4/{kind}"},
                                    "status": 200, "durationMs": 42, "requestId": "mock-request", "response": body})
        if route == "/samples":
            return self._json(200, {"samples": [{"key": f"samples/{n}", "name": n, "sizeBytes": 1, "url": ""} for n in
                                                ("consult-01-knee-pain-blood-pressure.wav",
                                                 "consult-01-knee-pain-blood-pressure-stereo.wav")]})
        if route == "/drafts":
            return self._json(200, {"drafts": [dict(d["item"], proposedCount=len(d["draft"]["proposed"]),
                                                    flaggedCount=0, excludedCount=len(d["draft"]["excluded"]))
                                               for d in DRAFTS.values()]})
        if route.startswith("/drafts/") and route.count("/") == 2 and route.split("/")[2] in DRAFTS:
            return self._json(200, draft_response(route.split("/")[2]))
        return self._json(404, {"error": "NOT_FOUND", "message": "Not mocked."})

    def do_POST(self):
        if self.path.startswith(COGNITO):
            body = self._body()
            return self._json(200, {"AuthenticationResult": {"IdToken": fake_jwt(body["AuthParameters"]["USERNAME"])}})
        route = self.path[len(API):].split("?")[0]
        parts = route.split("/")
        if route in ("/process", "/process/upload"):
            return self._json(202, {"draftId": "d-2", "status": "UPLOADED"})
        if len(parts) >= 4 and parts[1] == "drafts" and parts[2] in DRAFTS:
            d = DRAFTS[parts[2]]
            if parts[3] == "notes" and len(parts) == 4:
                PENDING_NOTES.setdefault(parts[2], {})[self._body()["template"]] = 0
                return self._json(202, {"status": "GENERATING"})
            if parts[3] == "notes" and parts[-1] == "use":
                ctx = {"patientId": PID, "encounterId": parts[2], "now": "2026-11-27T10:05:00Z"}
                item = dict(note_reference(d["notes"][parts[4]], ctx), id="documentreference-2")
                d["extra"]["noteTemplate"] = parts[4]
                return self._json(200, {"item": item})
            if parts[3] == "items":
                try:
                    return self._json(201, {"item": add_item(d["draft"], self._body(), "2026-11-27T10:05:00Z")})
                except EditError as exc:
                    return self._json(exc.status, {"error": exc.code, "message": exc.message})
            if parts[3] in ("approve", "reject"):
                return self._json(200, {"draft": dict(d["item"], status="WRITTEN" if parts[3] == "approve" else "REJECTED"),
                                        "written": [{"proposedId": "encounter", "resourceType": "Encounter", "id": parts[2]}]})
        return self._json(404, {"error": "NOT_FOUND", "message": "Not mocked."})

    def do_PUT(self):
        parts = self.path[len(API):].split("/")
        if "encounters" in parts:
            return self._json(200, {"versionId": "2", "provenance": "Provenance/mock", "period": self._body()})
        if len(parts) == 5 and parts[3] == "items" and parts[2] in DRAFTS:
            try:
                return self._json(200, {"item": edit_item(DRAFTS[parts[2]]["draft"], parts[4], self._body())})
            except EditError as exc:
                return self._json(exc.status, {"error": exc.code, "message": exc.message})
        return self._json(404, {"error": "NOT_FOUND", "message": "Not mocked."})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--overlay", type=Path)
    args = parser.parse_args()
    Handler.overlay = args.overlay
    print(f"Mock voice-to-FHIR on http://127.0.0.1:{args.port}/ (synthetic data, no AWS)")
    ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, directory=str(ROOT / "frontend"))).serve_forever()


if __name__ == "__main__":
    main()
