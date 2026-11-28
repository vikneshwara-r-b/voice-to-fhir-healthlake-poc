// Entry point: session lifecycle, hash routing and the Start -> Status -> Review -> Done flow.
// Routes: #/start (new consultation), #/drafts (all drafts), #/drafts/<id> (status, review or result).
import { api, ApiError } from "./api.js";
import { AuthError, currentSession, isConfigured, signIn, signOut } from "./auth.js";
import { renderBanner, renderChart } from "./chart.js";
import { openEditor } from "./edit.js";
import { openEncounterEditor, openFhirViewer } from "./record.js";
import { renderCompare, renderNotes, showEvidence } from "./scribe.js";
import * as v from "./views.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 5000;
const UPLOAD_TYPES = { wav: "audio/wav", mp3: "audio/mpeg", flac: "audio/flac", m4a: "audio/mp4" };
const MAX_UPLOAD_BYTES = 4 * 1024 * 1024;
const state = { draftId: null, review: null, pollTimer: null, lastStatus: null, patients: null, flash: null,
  patientId: null, chartToken: 0 };

const errorText = (err) => (err instanceof ApiError || err instanceof AuthError ? err.message : "Something went wrong. Try again.");

function stopPolling() {
  clearTimeout(state.pollTimer);
  clearTimeout(state.notePoll);
  state.pollTimer = state.notePoll = null;
  state.draftId = null;
}

// --- routing -------------------------------------------------------------------------------
function navigate(hash, flash) {
  state.flash = flash || null;
  if (location.hash === hash) route();
  else location.hash = hash;
}

function route() {
  if (!currentSession()) return;
  stopPolling();
  document.querySelectorAll("audio").forEach((player) => player.pause());
  v.clearAlerts();
  if (state.flash) v.showAlert(state.flash[0], state.flash[1]);
  state.flash = null;
  const hash = location.hash || "#/start";
  const match = hash.match(/^#\/drafts\/([A-Za-z0-9-]{1,64})$/);
  v.setActiveNav(match || hash === "#/drafts" ? "drafts" : "start");
  if (match) startStatus(match[1]);
  else if (hash === "#/drafts") showDrafts();
  else showStart();
}

window.addEventListener("hashchange", route);

async function loadPatients() {
  if (!state.patients) state.patients = (await api.patients()).patients;
  return state.patients;
}

const patientName = (id) => state.patients?.find((p) => p.id === id)?.name;

// --- session -------------------------------------------------------------------------------
function enterApp(session) {
  v.setSignedInUser(session);
  route();
}

function returnToLogin(message) {
  stopPolling();
  signOut();
  state.patients = null;
  v.setSignedInUser(null);
  v.clearAlerts();
  if (message) v.showAlert("info", message);
  $("login-form").reset();
  $("login-form").classList.remove("was-validated");
  v.showView("login");
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  v.setLoginError(null);
  form.classList.add("was-validated");
  if (!form.checkValidity()) return;
  v.setBusy($("login-submit"), true);
  try {
    const session = await signIn(form.email.value.trim(), form.password.value);
    form.reset();
    form.classList.remove("was-validated");
    enterApp(session);
  } catch (err) {
    v.setLoginError(errorText(err));
  } finally {
    v.setBusy($("login-submit"), false);
  }
});

$("btn-signout").addEventListener("click", () => returnToLogin("You have signed out."));
window.addEventListener("auth:expired", () => returnToLogin("Your session has expired. Sign in again."));

// --- start ---------------------------------------------------------------------------------
async function showStart() {
  v.showView("start");
  v.loading($("patient-list"), "Loading patients…");
  const [patients, samples] = await Promise.allSettled([loadPatients(), api.samples()]);
  if (patients.status === "fulfilled") v.renderPatients(patients.value);
  else { $("patient-list").replaceChildren(); v.showAlert("danger", `Could not load patients: ${errorText(patients.reason)}`); }
  if (samples.status === "fulfilled") v.renderSamples(samples.value.samples);
  else v.showAlert("danger", `Could not load samples: ${errorText(samples.reason)}`);
  checkUpload();
  const previous = document.querySelector(`#patient-list input[value="${CSS.escape(state.patientId || "")}"]`);
  if (previous) previous.checked = true;
  selectPatient(previous ? state.patientId : null);
}

// Patient chart (EHR-style sections), loaded when a patient is selected.
async function selectPatient(patientId) {
  state.patientId = patientId;
  const token = ++state.chartToken;
  $("chart-empty").hidden = Boolean(patientId);
  $("chart").hidden = !patientId;
  if (!patientId) return;
  $("patient-required").hidden = true;
  const known = state.patients?.find((p) => p.id === patientId);
  if (known) renderBanner(known);
  v.loading($("chart-sections"), "Loading chart…");
  try {
    const chart = await api.patientChart(patientId);
    if (token === state.chartToken) renderChart(chart);
  } catch (err) {
    if (token !== state.chartToken) return;
    $("chart-sections").replaceChildren();
    v.showAlert("danger", `Could not load the patient chart: ${errorText(err)}`);
  }
}

$("patient-list").addEventListener("change", (event) => {
  if (event.target.name === "patient") selectPatient(event.target.value);
});
$("chart-banner").addEventListener("click", (event) => {
  if (event.target.closest("[data-action=fhir-view]")) openFhirViewer(state.patientId);
});
$("chart-sections").addEventListener("click", (event) => {
  const button = event.target.closest("[data-encounter-id]");
  if (!button) return;
  const patientId = state.patientId;
  openEncounterEditor(patientId, button, (result) => {
    v.showAlert("success", `Encounter updated in HealthLake (version ${result.versionId}); change recorded in ${result.provenance}.`);
    selectPatient(patientId);
  });
});

// Upload (synthetic recordings only): checked here and again by the API.
const chosenFile = () => $("upload-file").files[0] || null;
const fileType = (file) => UPLOAD_TYPES[file.name.split(".").pop().toLowerCase()];

function checkUpload() {
  const file = chosenFile();
  let problem = "";
  if (file && !fileType(file)) problem = "Choose a WAV, MP3, FLAC or M4A file.";
  else if (file && file.size > MAX_UPLOAD_BYTES) problem = "The file is larger than 4 MB.";
  else if (file && !file.size) problem = "The file is empty.";
  $("upload-file").setCustomValidity(problem);
  $("upload-feedback").textContent = problem;
  $("upload-synthetic").required = Boolean(file);
  $("sample-select").required = !file;
  $("sample-select").disabled = Boolean(file);
  updatePreview();
}

function updatePreview() {
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  const file = chosenFile();
  state.previewUrl = file && fileType(file) ? URL.createObjectURL(file) : null;
  const sampleUrl = file ? null : $("sample-select").selectedOptions[0]?.dataset.url;
  v.setAudio($("start-audio"), state.previewUrl || sampleUrl || null);
}

$("upload-file").addEventListener("change", checkUpload);

// AI engine: Transcribe Medical, AWS HealthScribe, or both (HealthScribe adds a note template and pronouns).
const chosenEngine = () => document.querySelector("input[name=engine]:checked")?.value || "transcribe";
function engineOptions() {
  const engine = chosenEngine();
  if (engine === "transcribe") return { engine };
  const pronouns = $("pronouns").value;
  return { engine, noteTemplate: $("note-template").value, ...(pronouns ? { pronouns } : {}) };
}
document.querySelectorAll("input[name=engine]").forEach((radio) => radio.addEventListener("change", () => {
  $("scribe-options").hidden = chosenEngine() === "transcribe";
}));
$("sample-select").addEventListener("change", updatePreview);

$("process-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const patient = document.querySelector("input[name=patient]:checked");
  $("patient-required").hidden = Boolean(patient);
  form.classList.add("was-validated");
  if (!patient || !form.checkValidity()) return;
  v.setBusy($("process-submit"), true);
  try {
    const file = chosenFile();
    const options = engineOptions();
    const { draftId } = file
      ? await api.upload(patient.value, file, fileType(file), options)
      : await api.process(patient.value, $("sample-select").value, options);
    form.reset();
    form.classList.remove("was-validated");
    checkUpload();
    navigate(`#/drafts/${draftId}`);
  } catch (err) {
    v.showAlert("danger", errorText(err));
  } finally {
    v.setBusy($("process-submit"), false);
  }
});

// --- drafts --------------------------------------------------------------------------------
async function showDrafts() {
  v.showView("drafts");
  v.loading($("drafts-body"), "Loading drafts…");
  v.setBusy($("btn-drafts-refresh"), true);
  const [drafts] = await Promise.allSettled([api.drafts(), loadPatients()]);
  v.setBusy($("btn-drafts-refresh"), false);
  if (drafts.status === "fulfilled") v.renderDrafts(drafts.value.drafts, patientName);
  else { $("drafts-body").replaceChildren(); v.showAlert("danger", `Could not load drafts: ${errorText(drafts.reason)}`); }
}

$("btn-drafts-refresh").addEventListener("click", () => { v.clearAlerts(); showDrafts(); });

// --- status (polls every 5 s) --------------------------------------------------------------
function startStatus(draftId) {
  state.draftId = draftId;
  state.lastStatus = null;
  v.renderStatus(draftId, "UPLOADED");
  v.showView("status");
  poll();
}

async function poll() {
  const draftId = state.draftId;
  try {
    const [data] = await Promise.all([api.draft(draftId), loadPatients().catch(() => null)]);
    if (draftId !== state.draftId) return;
    const { status, errorCode } = data.draft;
    if (status !== state.lastStatus) v.renderStatus(draftId, status, errorCode);
    v.renderEngines(data.draft);
    state.lastStatus = status;
    if (data.proposed) return showReview(data);
    if (status === "FAILED") return;
  } catch (err) {
    if (draftId !== state.draftId || err.status === 401) return;
    if (err.status === 404) return v.showAlert("danger", "Draft not found.");
    v.showAlert("warning", `Status check failed (${errorText(err)}). Retrying…`);
  }
  state.pollTimer = setTimeout(poll, POLL_MS);
}

// --- review --------------------------------------------------------------------------------
function updateSelectionCount() {
  const count = v.selectedResourceIds().length;
  $("selection-count").textContent = `${count} of ${state.review.proposed.length} selected`;
  $("btn-approve").disabled = count === 0 || !["PENDING_REVIEW", "APPROVED"].includes(state.review.draft.status);
}

function showReview(data) {
  clearTimeout(state.pollTimer);
  state.review = data;
  const { status } = data.draft;
  if (status === "WRITTEN") return showDone(writtenFromIds(data.draft.healthLakeIds || {}));
  const readOnly = !["PENDING_REVIEW", "APPROVED"].includes(status);
  v.renderReview(data, readOnly, patientName(data.draft.patientId));
  showNotes(null);
  renderCompare(data.comparison);
  pollNotes();
  $("review-encounter-start").value = v.toIstInput(data.draft.createdAt);
  $("review-encounter-start").disabled = readOnly;
  if (status === "APPROVED") v.showAlert("warning", "Writing to HealthLake did not finish. Approve again to resume; already written resources are not written twice.");
  if (status === "REJECTED") {
    const feedback = data.review?.feedback;
    v.showAlert("info", `This draft was rejected. Nothing was written to HealthLake.${feedback ? ` Feedback: ${feedback}` : ""}`);
  }
  window.bootstrap.Tab.getOrCreateInstance($("tab-transcript")).show();
  v.showView("review");
  updateSelectionCount();
}

$("resource-list").addEventListener("change", updateSelectionCount);
$("resource-list").addEventListener("click", (event) => {
  const editId = event.target.closest("[data-edit]")?.dataset.edit;
  if (editId) return edit({ item: state.review.proposed.find((p) => p.id === editId) });
  const id = event.target.closest("[data-json]")?.dataset.json;
  if (!id) return;
  v.showJson(state.review.proposed, id);
  window.bootstrap.Tab.getOrCreateInstance($("tab-json")).show();
});

// Add / edit: the saved item replaces (or joins) the list; ticks the user already made are kept.
function applySaved(saved, added, position) {
  const selected = new Set(v.selectedResourceIds());
  const list = state.review.proposed;
  const index = list.findIndex((p) => p.id === saved.id);
  if (index >= 0) list[index] = saved;
  else list.splice(position ?? list.length, 0, saved);
  if (added) selected.add(saved.id);
  v.renderResources(list, false, selected);
  updateSelectionCount();
}

function edit({ item, kind }) {
  openEditor({ draftId: state.review.draft.draftId, item, kind, onSaved: (saved, added) => {
    applySaved(saved, added);
    v.showAlert("success", `${added ? "Added" : "Saved"}: ${saved.label}. It is not written to HealthLake until you approve.`);
  } });
}

// --- clinical note (AWS HealthScribe) --------------------------------------------------------
const reviewReadOnly = () => !["PENDING_REVIEW"].includes(state.review.draft.status);
const showNotes = (wanted) => { state.noteShown = renderNotes(state.review, wanted, reviewReadOnly()); };

function pollNotes() {
  clearTimeout(state.notePoll);
  if (!(state.review.draft.pendingNotes || []).length) return;
  const draftId = state.review.draft.draftId;
  state.notePoll = setTimeout(async () => {
    try {
      const data = await api.draft(draftId);
      if (state.review?.draft.draftId !== draftId) return;
      Object.assign(state.review.draft, { pendingNotes: data.draft.pendingNotes, lastNoteError: data.draft.lastNoteError });
      state.review.notes = data.notes || {};
      showNotes(state.noteShown);
    } catch { /* keep polling */ }
    pollNotes();
  }, POLL_MS);
}

$("note-select").addEventListener("change", (e) => showNotes(e.target.value));
$("note-sections").addEventListener("click", (event) => {
  const button = event.target.closest(".note-sentence");
  if (button) showEvidence(state.review.notes[state.noteShown], button);
});
$("btn-generate-note").addEventListener("click", async () => {
  const template = $("note-new-template").value;
  v.setBusy($("btn-generate-note"), true);
  try {
    await api.generateNote(state.review.draft.draftId, template);
    state.review.draft.pendingNotes = [...new Set([...(state.review.draft.pendingNotes || []), template])];
    showNotes(state.noteShown);
    pollNotes();
  } catch (err) {
    v.showAlert("danger", errorText(err));
  } finally {
    v.setBusy($("btn-generate-note"), false);
  }
});
$("btn-use-note").addEventListener("click", async () => {
  const template = state.noteShown;
  v.setBusy($("btn-use-note"), true);
  try {
    const { item } = await api.useNote(state.review.draft.draftId, template);
    const existed = state.review.proposed.some((p) => p.id === item.id || p.noteTemplate);
    state.review.proposed = state.review.proposed.filter((p) => !p.noteTemplate || p.id === item.id);
    applySaved(item, !existed, 1);
    state.review.noteTemplate = template;
    showNotes(template);
    v.showAlert("success", `The ${item.label} is now the proposed clinical note. Tick it on the FHIR resources tab to write it.`);
  } catch (err) {
    v.showAlert("danger", errorText(err));
  } finally {
    v.setBusy($("btn-use-note"), false);
  }
});

$("btn-add-condition").addEventListener("click", () => edit({ kind: "Condition" }));
$("btn-add-medication").addEventListener("click", () => edit({ kind: "MedicationStatement" }));
$("json-select").addEventListener("change", (e) => v.showJson(state.review.proposed, e.target.value));

$("btn-approve").addEventListener("click", async () => {
  const ids = v.selectedResourceIds();
  if (!ids.length) return v.showAlert("warning", "Select at least one resource to approve.");
  if (!$("review-encounter-start").checkValidity()) {
    $("review-encounter-start").focus();
    return v.showAlert("warning", "Enter the consultation date and time.");
  }
  const { ok, feedback } = await v.confirmAction({
    title: "Write to HealthLake?",
    body: `${ids.length} selected resource${ids.length === 1 ? "" : "s"} will be written to AWS HealthLake, plus one Encounter and Provenance records (AI pipeline as author, you as verifier; you as author of anything you added). Unselected and excluded items are not written.`,
    okLabel: `Write ${ids.length} resource${ids.length === 1 ? "" : "s"}`,
    okClass: "btn-primary",
    feedback: { label: "Feedback on the AI draft (optional)", required: false },
  });
  if (!ok) return;
  v.setBusy($("btn-approve"), true);
  try {
    const result = await api.approve(state.review.draft.draftId, ids, feedback || undefined,
      v.fromIstInput($("review-encounter-start").value));
    v.clearAlerts();
    showDone(result.written);
  } catch (err) {
    v.showAlert("danger", errorText(err));
    v.setBusy($("btn-approve"), false);
  }
});

$("btn-reject").addEventListener("click", async () => {
  const { ok, feedback } = await v.confirmAction({
    title: "Reject this draft?", body: "Nothing will be written to HealthLake. This cannot be undone.",
    okLabel: "Reject draft", okClass: "btn-danger",
    feedback: { label: "Why are you rejecting this draft? (required)", required: true },
  });
  if (!ok) return;
  v.setBusy($("btn-reject"), true);
  try {
    await api.reject(state.review.draft.draftId, feedback);
    navigate("#/drafts", ["info", "Draft rejected. Nothing was written to HealthLake."]);
  } catch (err) {
    v.showAlert("danger", errorText(err));
  } finally {
    v.setBusy($("btn-reject"), false);
  }
});

// --- done ----------------------------------------------------------------------------------
function writtenFromIds(ids) {
  return Object.values(ids).map((ref) => {
    const [resourceType, id] = ref.split("/");
    return { resourceType, id };
  });
}

function showDone(written) {
  clearTimeout(state.pollTimer);
  state.patients = null;  // problem counts changed; reload on the next visit to Patients
  v.renderDone(written);
  v.showView("done");
  v.setBusy($("btn-approve"), false);
}

// --- boot ----------------------------------------------------------------------------------
if (!isConfigured()) {
  v.showAlert("danger", "config.js has no Cognito settings. Deploy the stack or edit js/config.js for local testing.");
}
const session = currentSession();
if (session) enterApp(session);
else v.showView("login");
