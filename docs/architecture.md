# Architecture

Reference for how voice-to-fhir-healthlake-poc works. Overview, deploy and demo steps: [../README.md](../README.md).

**Contents:** [Stacks and resources](#stacks-and-resources) · [Data layout](#data-layout) ·
[Pipeline and engines](#pipeline-and-engines) · [AWS HealthScribe](#aws-healthscribe) ·
[FHIR mapping rules](#fhir-mapping-rules) · [Review, edits and feedback](#review-edits-and-feedback) ·
[Approve → HealthLake](#approve--healthlake) · [Patient chart and record tools](#patient-chart-and-record-tools) ·
[API](#api) · [Web UI](#web-ui) · [Security and privacy](#security-and-privacy) · [Design decisions](#design-decisions)

## Stacks and resources

Generated from the CDK app with CDK Graph (`scripts/generate_architecture_diagram.py`):
[overview](images/architecture.svg) (main components only: no IAM, log groups, CloudFront plumbing or CDK
helper resources) and [detailed](images/architecture-detailed.svg) (every resource). A hand-drawn, slide-ready
diagram of the end-to-end flow: [architecture-presentation.svg](images/architecture-presentation.svg) /
[.png](images/architecture-presentation.png).

| Stack | Contents |
|---|---|
| `VoiceToFhir-<env>` | Cognito user pool, SPA client and demo user; private data bucket; `drafts` DynamoDB table; pipeline Lambdas and S3 notifications; HealthScribe data access role; HTTP API and its Lambda; private site bucket, CloudFront (OAC) and security-headers policy; site and sample deployments |
| `VoiceToFhirHealthLake-<env>` | Optional: an empty FHIR R4 datastore (SigV4), created only when `existingHealthLakeDatastoreId` is not given and deployed only when named |

| Lambda (Python 3.12, arm64) | Trigger | Timeout / memory | Job |
|---|---|---|---|
| `start_transcription` | S3 `audio/` + `.wav`/`.mp3`/`.flac`/`.m4a` | 30 s / 256 MB | Start the chosen engine(s) |
| `analyze_transcript` | S3 `transcripts/` + `.json` | 120 s / 512 MB | Transcribe Medical engine: parse, Comprehend Medical, finish engine |
| `scribe_watcher` | async invoke, no retries | 900 s / 256 MB | HealthScribe engine: poll job, parse, note, Comprehend Medical, finish engine |
| `api` | HTTP API (JWT) | 20 s / 512 MB | All API routes |

CDK also adds its own helper Lambdas (bucket notifications, auto-delete, BucketDeployment). Logs keep 1 week.

## Data layout

**S3 data bucket** (private, SSE-S3, SSL only):

| Key | Written by | Contents |
|---|---|---|
| `samples/*.wav` | CDK BucketDeployment | Bundled synthetic recordings |
| `audio/{patientId}/{draftId}/consult\|upload.<ext>` | API (`/process`, `/process/upload`) | The consultation audio. Triggers `start_transcription` |
| `transcripts/{draftId}.json` | Transcribe Medical | Raw Transcribe Medical output. Triggers `analyze_transcript` |
| `<prefix>-scribe-{draftId}-{template}-{run}/transcript.json`, `…/summary.json` | AWS HealthScribe | Raw HealthScribe transcript and clinical documentation (confirmed layout). `{template}` is lower case without underscores and `{run}` is 0 for the first job, e.g. `voice-to-fhir-dev-scribe-<draftId>-historyandphysical-0` |
| `results/{draftId}/{transcribe\|healthscribe}.json` | engine Lambdas | Per-engine turns, NLP, roles method; HealthScribe also insights and note |
| `notes/{draftId}/{template}.json` | `scribe_watcher` | Parsed HealthScribe note: sections, sentences, evidence (role + text) |
| `nlp/{draftId}.json` | assembly | Comprehend Medical output of the primary engine (offsets rebased, turn index) |
| `drafts/{draftId}.json` | assembly, API | The draft: turns, proposed and excluded resources, comparison, review feedback |
| `.write_access_check_file.temp` | Transcribe / HealthScribe | 0-byte write check at the bucket root (no notification covers it) |

**DynamoDB `drafts` table** (key `draftId` = encounter id). Status, ids, counts and codes only: no transcript,
entity or feedback text. Fields: `status`, `patientId`, `encounterId`, `audioKey`, `source` (sample/upload),
`sampleKey`, `jobName`, `engine`/`engines`, `pendingEngines` (string set), `noteTemplate`, `pronouns`, `channels`,
`pendingNotes`, `errorCode`, `lastNoteError`, `lastErrorCode`, counts (`turnCount`, `entityCount`,
`proposedCount`, `flaggedCount`, `excludedCount`), `approvedIds`, `approvedBy`, `healthLakeIds`, `lockUntil`,
`reviewedBy`, `hasFeedback`, `createdBy`, and timestamps (`createdAt`, `updatedAt`, `approvedAt`, `reviewedAt`,
`writtenAt`).
Statuses: `UPLOADED`, `TRANSCRIBING`, `ANALYZING`, `PENDING_REVIEW`, `APPROVED`, `REJECTED`, `WRITTEN`, `FAILED`
(see the lifecycle diagram in the README). Every status change is a conditional write.

## Pipeline and engines

```
POST /process or /process/upload  → draft UPLOADED {engine, noteTemplate, pronouns}; audio → audio/…
audio/ event → start_transcription
    UPLOADED|TRANSCRIBING → TRANSCRIBING, pendingEngines = {chosen engines}, channels (WAV header)
    transcribe:   StartMedicalTranscriptionJob  (PRIMARYCARE, CONVERSATION; 2 speaker labels or
                  ChannelIdentification for stereo) → transcripts/{draftId}.json
    healthscribe: StartMedicalScribeJob (data access role, note template, pronouns, speaker labels or
                  ChannelDefinitions) + async invoke scribe_watcher
transcripts/ event → analyze_transcript → turns → Comprehend Medical → finish_engine("transcribe")
scribe_watcher → poll GetMedicalScribeJob → read reported output → note → Comprehend Medical
               → finish_engine("healthscribe")
finish_engine: write results/{draftId}/{engine}.json; remove engine from pendingEngines (atomic);
               the engine that empties the set assembles drafts/ + nlp/ and sets PENDING_REVIEW
```

- **Engines:** `transcribe` (default), `healthscribe`, or `both`. With both, HealthScribe is the **primary** engine
  (proposed FHIR, NLP tab, note), and the Transcribe Medical result is kept only for the comparison.
- **Speaker roles:** Transcribe Medical labels the first speaker Clinician. Stereo audio uses channel 0 =
  Clinician, channel 1 = Patient. HealthScribe assigns `CLINICIAN_n` / `PATIENT_n` itself.
- **Comprehend Medical:** DetectEntitiesV2 (up to 20,000 bytes), InferICD10CM and InferRxNorm (up to 10,000
  characters each) run per chunk. Chunks are consecutive speaker turns of at most 9,000 UTF-8 bytes; offsets are
  rebased onto the full transcript.
- **Idempotency:** duplicate S3 events and repeated starts are safe (conditional writes; deterministic job names
  return `ConflictException`). `finish_engine` is guarded by `contains(pendingEngines, engine)`, so each engine
  completes once and the draft is assembled once.
- **No self-triggering:** every notification has a prefix and a suffix, and no Lambda writes to the prefix that
  triggers it. HealthScribe output is never event-driven.
- **Failures:** `FAILED` plus a short code (`COPY_FAILED`, `TRANSCRIBE_…`, `ANALYZE_…`, `SCRIBE_JOB_FAILED`,
  `SCRIBE_TIMEOUT`, …).
  A failed Transcribe Medical job writes no transcript, so `GET /drafts/{id}` checks the job while `TRANSCRIBING`.

## AWS HealthScribe

HealthScribe is generative AI. Its output is labelled AI-generated and written only after clinician approval.
It runs in us-east-1, US English.

| Capability | API detail | Where it shows |
|---|---|---|
| Speaker roles | `ParticipantDetails.ParticipantRole` | Transcript tab; replaces "first speaker = clinician" |
| Dialogue classification | `SectionDetails.SectionName` (e.g. `SUBJECTIVE`, `SMALL_TALK`) | Transcript tab: section badge per turn |
| Clinical insights | `ClinicalInsights` (category, type, spans, attributes; **no codes**) | Compare engines tab |
| Clinical note | `ClinicalNoteGenerationSettings.NoteTemplate`: `HISTORY_AND_PHYSICAL` (default), `PHYSICAL_SOAP`, `BEHAVIORAL_SOAP`, `DAP`, `GIRPP`, `BIRP`, `SIRP` | Clinical note tab; proposed DocumentReference (LOINC 34117-2 for H&P, 11506-3 otherwise), flagged and unticked |
| Evidence links | `Summary[].EvidenceLinks[].SegmentId` | Clinical note tab: a sentence shows the dialogue it came from |
| Another template | new job on the same audio (`POST /drafts/{id}/notes`) | Clinical note tab: **Generate**, then **Use this note for HealthLake** |
| Patient context | `MedicalScribeContext.PatientContext.Pronouns` | Start: optional pronouns, never inferred |
| Channel identification | `Settings.ChannelIdentification` + `ChannelDefinitions` | Stereo sample |

- **Watcher:** `StartMedicalScribeJob` has no output-key parameter, so `scribe_watcher` polls `GetMedicalScribeJob`
  every 15 s (stopping about 3 minutes before its 15-minute limit). It reads the `TranscriptFileUri` and
  `ClinicalDocumentUri` the job reports, and only accepts this job's own keys in our bucket.
- **Comprehend Medical** runs on the HealthScribe transcript too, because HealthScribe gives no ICD-10/RxNorm codes
  and no negation/family-history traits.
- **Comparison (Both):** the FHIR mapping is run on each engine's result to list what each would propose, next to
  the role method, turn and word counts, insights and both transcripts.

## FHIR mapping rules

| Rule | Detail |
|---|---|
| DocumentReference (transcript) | Labelled transcript ("Clinician: …") as base64 `text/plain`. LOINC 11506-3 Progress note, `status: current`, subject, `context.encounter`, description names the engine. Flagged if over 10,000 characters (HealthLake note limit). Pre-selected |
| DocumentReference (HealthScribe note) | Plain-text note with section headings, LOINC per template, flagged `AI_GENERATED_NOTE`, **not** pre-selected |
| Condition | InferICD10CM `MEDICAL_CONDITION` entity with a `DIAGNOSIS`, `SIGN` or `SYMPTOM` trait and an ICD-10-CM code (top concept). `encounter-diagnosis`, `clinicalStatus: active`, `verificationStatus: provisional` (`differential` when `HYPOTHETICAL`) |
| MedicationStatement | InferRxNorm `MEDICATION` entity with an RxNorm code. `completed` for `PAST_HISTORY`, else `active`. Dosage text from strength, dose, route, frequency |
| Excluded (never written) | `NEGATION`, `PERTAINS_TO_FAMILY`, conditions without diagnosis/sign/symptom, no code. Shown with the reason; repeated mentions are listed once with a count |
| Flagged (not pre-selected) | `HYPOTHETICAL` or `LOW_CONFIDENCE` trait, entity score < 0.80 (`CONFIDENCE_THRESHOLD`), or top code score < 0.30 (`CODE_SCORE_FLOOR`; code scores are usually 0.3–0.6 even when correct) |
| De-duplication | One resource per code; the unflagged, highest-confidence mention wins |
| AI labelling | `meta.security` `v3-ObservationValue#AIAST`, plus an extension `http://example.org/fhir/StructureDefinition/ai-confidence` when a confidence is known |
| Validation | Structural checks before saving a draft and again before writing (references, codings, required fields, no empty values); HealthLake's strict validation applies on write |

## Review, edits and feedback

- **Edit** (`PUT /drafts/{id}/items/{itemId}`): Condition (ICD-10-CM code, description, clinical and verification
  status), MedicationStatement (RxNorm code, description, status including `stopped`, dosage), DocumentReference
  text. The browser sends named fields only; the API builds and validates the FHIR. Edited AI resources keep
  AIAST and gain `meta.tag` `clinician-edited`; the original is kept as `aiResource`.
- **Add** (`POST /drafts/{id}/items`): Condition or MedicationStatement tagged `clinician-entered`, with no AIAST.
- **Feedback:** required to reject (5–2,000 characters), optional on approve. It is stored only in the draft JSON
  in S3 (`review`), never in DynamoDB (`hasFeedback` only), logs or HealthLake.
- **Consultation date and time** on Review becomes the Encounter `period.start` on approval.
- Changes are allowed only while `PENDING_REVIEW`. With one demo clinician, the last save wins.

## Approve → HealthLake

1. Check the selection (ids must be in the draft) and validate the Encounter and every selected resource. Any
   problem blocks all writes (400/422).
2. `PENDING_REVIEW → APPROVED` (conditional), storing the selection and the clinician's Cognito `sub`, with a
   60-second write lock. Optional feedback is saved with the draft.
3. Write with SigV4-signed FHIR REST (botocore signer + `urllib`), recording each HealthLake id in `healthLakeIds`
   as it is created:
   - `PUT Encounter/{encounterId}` (finished, ambulatory, `period.start` = consultation time), so every
     `Encounter/…` reference resolves;
   - `POST` each selected resource in draft order;
   - `POST Provenance` for AI-derived resources: author = "AI-assisted pipeline (<engine>)", verifier = the
     clinician (Cognito issuer + `sub`), activity `CREATE`;
   - a second `POST Provenance` for clinician-entered resources (clinician as author, no AIAST).
4. `APPROVED → WRITTEN`.

Approving again with the same selection returns the stored ids and writes nothing. A 5xx, 429 or network failure
leaves `APPROVED` and releases the lock, so approving again resumes. A 4xx sets `FAILED`
(`HEALTHLAKE_REJECTED_<status>`). A concurrent approve gets 409.

## Patient chart and record tools

- **Chart** (`GET /patients/{id}/summary`): six HealthLake searches in parallel (Condition, MedicationRequest,
  MedicationStatement, Observation, Encounter, DocumentReference). Sections: problem list (active first), medications
  (prescribed and reported), latest vitals and labs per code, encounters (newest 10), notes. Each item is tagged
  `ai`, `clinician` or `record`.
- **View FHIR record** (`GET /patients/{id}/fhir?type=…`): one read-only signed request, returned with its URL,
  status, duration, HealthLake request id and the unmodified response. The browser never holds AWS credentials.
  (`Patient/$everything` is not supported by the datastore: "Interaction not supported".)
- **Edit encounter date** (`PUT /patients/{id}/encounters/{encounterId}`): read, check the patient, set `period`,
  PUT (new version), then `POST Provenance` (`UPDATE`, clinician as author, target `Encounter/{id}/_history/{n}`).

## API

HTTP API (`ApiUrl` output), every route behind the Cognito JWT authorizer (`Authorization: Bearer <ID token>`). CORS
allows only the CloudFront origin (GET, POST, PUT). The stage is throttled to 10 requests/s, burst 20.

| Route | Purpose |
|---|---|
| `GET /patients` | Patients (age, sex, DOB, active conditions) |
| `GET /patients/{id}/summary` | EHR-style chart |
| `GET /patients/{id}/fhir?type=<Type>` | Raw HealthLake request and response for this patient |
| `PUT /patients/{id}/encounters/{encounterId}` | `{start, end?}`: update the encounter period, plus Provenance |
| `GET /samples` | Sample audio, with presigned playback links (1 hour) |
| `POST /process` | `{patientId, sampleKey, engine?, noteTemplate?, pronouns?}` → `202 {draftId}` |
| `POST /process/upload?patientId=…&engine=…&noteTemplate=…&pronouns=…` | Raw audio body (WAV/MP3/FLAC/M4A, up to 4 MB; signature and WAV sample rate checked) → `202 {draftId}` |
| `GET /drafts` | Newest 50 drafts (status, engine, counts) |
| `GET /drafts/{id}` | Status, `audioUrl`; once analysed: turns, entities, proposed, excluded, notes, comparison, review |
| `POST /drafts/{id}/items` · `PUT /drafts/{id}/items/{itemId}` | Add / edit a proposed resource |
| `POST /drafts/{id}/notes` | `{template}`: run HealthScribe again with another template |
| `POST /drafts/{id}/notes/{template}/use` | Make that note the proposed clinical-note DocumentReference |
| `POST /drafts/{id}/reject` | `{feedback}` (required) |
| `POST /drafts/{id}/approve` | `{resourceIds, feedback?, encounterStart?}` |

Errors are JSON `{error, message}` with a short code. Response bodies never echo request data.

## Web UI

Plain HTML and ES modules with vendored Bootstrap 5.3.9: no framework, no build step, no `innerHTML` with data
(all dynamic text via `textContent`), no `alert()`/`confirm()`. Modules: `auth.js` (Cognito `InitiateAuth` with
`fetch`), `api.js`, `views.js`, `chart.js`, `edit.js`, `record.js`, `scribe.js`, `main.js` (hash routing).

- **Routes:** `#/start` (Patients), `#/drafts`, `#/drafts/<id>`. Back, reload and sign-in keep your place. A 401 returns
  to sign-in.
- **Patients:** patient list; chart banner with **View FHIR record in HealthLake**; **Start a consultation** (sample
  with preview player, or synthetic upload with a required confirmation; engine, note template, pronouns);
  section cards with **Edit date** on encounters.
- **Status:** progress bar, step badges, each engine shown as working or done, polling every 5 s, `aria-live`
  announcements, and a retry hint on failure.
- **Review:** audio player; consultation date and time; tabs Transcript, NLP entities (traits with scores, entity
  score, code score), FHIR resources (tick, Edit, Add, View JSON, excluded list), Clinical note (evidence,
  generate, use), Compare engines (Both only), JSON. Approve and reject use modals with feedback.
- **Done:** HealthLake ids written.
- **Look:** Bootstrap plus `css/custom.css`: navy header, white cards on a light background, initials badges for
  people, engine choices as selectable cards, transcript bubbles, and a review action bar (date, Approve, Reject) that
  stays in view on tablets and wider. Icons are inline SVG; no web fonts or external images, so the CSP stays `'self'`.
- **Time zone:** every time is shown and entered in IST (`Asia/Kolkata`, UTC+05:30), whatever the browser's zone
  (`TIME_ZONE` in `views.js`). The API, DynamoDB and HealthLake store ISO 8601 UTC instants (`…Z`); the browser
  converts both ways, and the FHIR viewer shows the raw JSON in UTC.
- **Accessibility:** labelled controls, keyboard operable, status never shown by colour alone, works on a laptop
  projector and on a phone.

## Security and privacy

- **Identity:** Cognito with self sign-up disabled; the demo password is a NoEcho CloudFormation parameter. The
  SPA client has no secret. Every API route requires the JWT.
- **Network:** CloudFront with OAC to a private site bucket; TLS-only buckets; CORS limited to the CloudFront
  origin; throttled API.
- **Browser:** the CSP allows `script-src 'self'`, `connect-src` Cognito + this region's API Gateway,
  `media-src` the data bucket's exact host + `blob:`, and `frame-ancestors 'none'`, plus HSTS, nosniff,
  no-referrer and DENY framing.
- **IAM (least privilege):**
  - each Lambda's S3 access is limited to its prefixes;
  - HealthLake actions (`SearchWithGet`, `ReadResource`, `CreateResource`, `UpdateResource`) only on the one
    datastore ARN;
  - Comprehend Medical and `StartMedicalTranscriptionJob` need `*` (no resource-level support);
  - `StartMedicalScribeJob` is conditioned on `transcribe:OutputBucketName`;
  - `iam:PassRole` is limited to the HealthScribe role and to Transcribe;
  - the HealthScribe role is assumable only by `transcribe.amazonaws.com` from this account, reads `audio/*` and
    writes only its job-name prefix.
- **PHI hygiene (synthetic data, treated as if real):** logs carry ids, codes and counts only. Feedback and note
  text stay in S3 draft objects. Uploads require a "synthetic" confirmation. Pronouns are never inferred.
- **Not included:** WAF, KMS customer-managed keys, VPC endpoints, audit trails beyond CloudWatch/CloudTrail
  defaults, multi-user authorisation. It is a demo.

## Design decisions

- **One main stack.** Constructs separate the concerns; one stack avoids cross-stack ordering problems. HealthLake
  sits in its own optional stack, so the app can be destroyed without touching the datastore.
- **Per-resource FHIR writes, not a transaction Bundle.** The documentation checked did not confirm `urn:uuid`
  resolution inside transactions. Per-resource writes with stored ids give resumable, idempotent approval.
- **HealthScribe watcher instead of S3 events.** There is no output-key parameter; polling the job and reading
  the URIs it reports does not depend on the output layout. The IAM scope relies on the confirmed
  `<job name>/…` layout.
- **CSP and the CloudFormation cycle.** The API's CORS origin references the CloudFront domain, so the CSP cannot
  name the exact API URL (the distribution would depend on the API). It allows this region's API Gateway hosts
  instead. The data bucket does not depend on the distribution, so `media-src` names it exactly.
- **Upload through the API, not presigned to S3.** A presigned upload needs an S3 CORS rule naming the CloudFront
  domain, while the CSP names the bucket: another cycle. The cost is the 4 MB limit.
- **Comprehend Medical alongside HealthScribe.** HealthScribe provides roles, sections, insights and notes, but no
  codes and no negation/family-history traits, which the FHIR mapping needs.
