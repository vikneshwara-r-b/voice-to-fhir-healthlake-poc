# Testing

How the project is tested, and a manual smoke checklist. Overview: [../README.md](../README.md).

## Automated tests (no AWS)

```bash
source .venv/bin/activate
pytest -q                 # unit + CDK assertion tests (169 at the time of writing)
```

| Suite | Tests | Covers |
|---|---|---|
| `tests/unit/test_transcript_parser.py` | 6 | Transcribe Medical output → turns; roles by speaker order; empty output |
| `tests/unit/test_nlp.py` | 4 | Chunking by speaker turn within byte limits (including multi-byte text); offset rebasing |
| `tests/unit/test_fhir_mapper.py` | 13 | Negation, family history, hypothetical, low confidence, code-score floor, de-duplication, DocumentReference, AI labels |
| `tests/unit/test_pipeline_handlers.py` | 13 | `start_transcription` / `analyze_transcript` with moto: job parameters, duplicate events, retryable vs failed errors, ignored keys, no text in logs |
| `tests/unit/test_scribe.py` | 15 | HealthScribe parsing (roles, sections, insights, note evidence), job settings (mono, stereo, pronouns), engine choice, watcher (success, failure, foreign output), both-engine assembly happening once, note re-runs, stereo Transcribe output |
| `tests/unit/test_api.py` | 43 | Every route: auth, validation, uploads, presigned links, drafts, edits, feedback storage (never in DynamoDB or logs), notes, FHIR view, encounter route |
| `tests/unit/test_approve.py` | 18 | Approve idempotency, resume after partial failure, write lock, permanent rejection, validation blocking all writes, Provenance groups, feedback, consultation time |
| `tests/unit/test_draft_edit.py`, `test_encounters.py`, `test_chart.py` | 15 + 10 + 2 | Clinician edits and adds, encounter date updates with versioned Provenance, chart sections |
| `tests/cdk/test_stack.py` | 29 | Private and SSL-only buckets; prefix + suffix on every notification; no Lambda writes to its trigger prefix; JWT on every route; CORS origin; throttling; HealthLake permissions scoped to the datastore; no wildcard actions; HealthScribe role and PassRole scoping; watcher settings; CSP; log retention; password parameter |

## Local UI mock (no AWS)

```bash
python tests/ui/mock_api.py            # http://127.0.0.1:8765/  (any email, any 8+ character password)
```

This serves the real `frontend/` with a local `config.js`, a mock Cognito sign-in and mock API routes. Draft data is
built by the real server code from synthetic inputs: a Transcribe Medical draft (`d-1`) and a Both-engines draft
(`d-2`) with a HealthScribe note. "Generate" another template completes after one poll. `--overlay DIR` serves extra
files first.

**Headless checks** used during development: a page in an overlay directory injects a session and a small
script that clicks through the UI and records results into the DOM. Then:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --virtual-time-budget=30000 \
  --enable-logging=stderr --dump-dom "http://127.0.0.1:8765/<page>.html#/drafts/d-2"
```

This verified the views, routing, edit and add, feedback modals, chart, FHIR viewer, encounter editing, notes with
evidence, the engine comparison, and that injected `<b>`/`<img onerror>` strings render as text. No Playwright
dependency was added. Checks run only against the mock, never against AWS.

## Scripts against a deployed stack

```bash
# Run one sample through the pipeline without the UI (Transcribe Medical engine); prints the draft summary.
python scripts/run_sample.py --profile $AWS_PROFILE --sample consult-01-knee-pain-blood-pressure.wav \
  --patient-id 0725e4075c0a604253ba23c24746cc8dea085e10cc6e9a51c309619a1d65137a

# API smoke test as the demo user: 401 without a token, then patients, samples, drafts; optionally process one.
python scripts/api_smoke.py --profile $AWS_PROFILE --email clinician.demo@example.com \
  [--process consult-02-gout-follow-up.wav --patient-prefix 69834515]
```

Both scripts create drafts, start AWS jobs (small cost) and use synthetic data only. `api_smoke.py` asks for the
password (or reads `V2F_DEMO_PASSWORD`), so it never appears on the command line.

## Manual smoke checklist (deployed stack)

**Sign-in**
1. Open `SiteUrl`: navbar, the yellow "Synthetic data only…" banner and a centred sign-in card.
2. Submit empty: inline validation, and no request is sent. Wrong password: "Incorrect email or password."
3. Sign in: the navbar shows **Patients**, **Drafts**, the user's name (**John Smith**, the Cognito `name`
   attribute; hover for the Cognito username) and **Sign out**. Reload: still signed in.
   Sign out: back to the sign-in card.

**Patients and records**
4. Patients: three patients with sex, age, DOB and an active-problem count. Select Elena: banner, Start a
   consultation, and six section cards. With only the loaded sample data: 7 problems, 7 medications,
   2 latest vitals, 15 latest labs, 3 encounters, no notes. Each approved consultation adds its Encounter,
   note(s) and any approved conditions and medications.
5. **View FHIR record in HealthLake**: `GET …/r4/Patient/<id>`, HTTP 200, a request id, "version … last updated …
   (set by HealthLake)". After an approval, type Condition shows the AI-written Condition with `AIAST`.
6. Encounters: times show in IST (for example "1 Nov 2025, 2:30 pm IST"). **Edit date** (entered in IST) → Save. A success alert names the new version and the Provenance, and the row shows
   the new time.

**Processing**
7. Choose a sample: a preview player plays it. Choose **AWS HealthScribe** or **Both**: the template and pronouns
   fields appear.
8. Process `consult-01…` with **Both**: Status moves Uploaded → Transcribing → Analysing → Review, and each engine
   is shown as working or done (about 2 minutes).
9. Upload: a file over 4 MB or a `.txt` shows an inline error. A WAV without the "synthetic" box ticked is refused.
   With the box ticked, it processes like a sample.
10. Stereo: process `…-stereo.wav`. The roles come from the audio channel.

**Review**
11. Transcript: the source and role method are stated, and HealthScribe drafts show a section per turn. The audio
    player works.
12. NLP entities: traits with scores, entity score, code and code score.
13. FHIR resources: the transcript is ticked; flagged items are unticked with "Flagged: …"; the clinical note is
    flagged "AI-generated note"; Excluded lists "Negated: fever" and "Family history: heart attack".
14. **Edit** a flagged condition (Confirmed, corrected description): "Edited" badge, and View JSON shows the
    `clinician-edited` tag. An invalid code (`hypertension`) is refused. **Add medication** (`860975`,
    "metformin 500 MG"): ticked, "Added by clinician".
15. Clinical note: a sentence shows its evidence. **Generate** another template ("generating…", then it
    appears); **Use this note for HealthLake** updates the proposed note.
16. Compare engines (Both): two cards with role method, counts, findings, insights and transcripts.
17. Reject without feedback: the modal will not close. With feedback: back to Drafts, the row shows "Rejected",
    and reopening shows the feedback.
18. Set **Consultation date and time**, tick items, **Approve** (optional feedback): the modal states the count.
    Cancel writes nothing. Confirm shows Done with the Encounter, resources and Provenance ids. The Patients
    chart now shows them badged.

**Navigation and accessibility**
19. Drafts: rows show the time in IST, patient, engine, status and counts. "← Back to drafts", browser Back and reload all land
    where expected.
20. Keyboard only: navbar, tabs, checkboxes, audio controls, forms, modals and Esc all work. Status changes are
    announced. At phone width there is no horizontal scrolling.
