// Patient record tools: raw HealthLake FHIR viewer and encounter date/time editor.
import { api } from "./api.js";
import { fromIstInput, istDateTime, setBusy, toIstInput } from "./views.js";

const $ = (id) => document.getElementById(id);
const modal = (id) => window.bootstrap.Modal.getOrCreateInstance($(id));
const ctx = { patientId: null, encounterId: null, onSaved: null };

// --- FHIR viewer ---------------------------------------------------------------------------
export function openFhirViewer(patientId) {
  ctx.patientId = patientId;
  $("fhir-meta").hidden = true;
  $("fhir-json").hidden = true;
  modal("fhir-modal").show();
  runRequest();
}

function summarize(body) {
  if (body.resourceType === "Bundle") return `Bundle with ${(body.entry || []).length} resource(s)`;
  const meta = body.meta || {};
  return body.resourceType ? `${body.resourceType} · version ${meta.versionId || "?"} · last updated ${meta.lastUpdated ? istDateTime(meta.lastUpdated) : "?"} (set by HealthLake)` : "–";
}

async function runRequest() {
  setBusy($("fhir-run"), true);
  try {
    const result = await api.fhirView(ctx.patientId, $("fhir-type").value);
    $("fhir-url").textContent = `${result.request.method} ${result.request.url}`;
    $("fhir-status").textContent = `HTTP ${result.status}${result.status >= 200 && result.status < 300 ? " (OK)" : ""}`;
    $("fhir-time").textContent = `${result.durationMs} ms (${result.request.signedWith})`;
    $("fhir-reqid").textContent = result.requestId || "–";
    $("fhir-summary").textContent = summarize(result.response || {});
    $("fhir-json").textContent = JSON.stringify(result.response, null, 2);
  } catch (err) {
    $("fhir-url").textContent = "";
    $("fhir-status").textContent = err.message;
    $("fhir-time").textContent = $("fhir-reqid").textContent = $("fhir-summary").textContent = "–";
    $("fhir-json").textContent = "";
  } finally {
    $("fhir-meta").hidden = false;
    $("fhir-json").hidden = !$("fhir-json").textContent;
    setBusy($("fhir-run"), false);
  }
}

$("fhir-form").addEventListener("submit", (event) => { event.preventDefault(); runRequest(); });

// --- encounter date/time -------------------------------------------------------------------
export function openEncounterEditor(patientId, button, onSaved) {
  Object.assign(ctx, { patientId, encounterId: button.dataset.encounterId, onSaved });
  $("encounter-type").textContent = button.dataset.type || "";
  $("encounter-start").value = toIstInput(button.dataset.start);
  $("encounter-end").value = toIstInput(button.dataset.end);
  $("encounter-error").hidden = true;
  $("encounter-form").classList.remove("was-validated");
  modal("encounter-modal").show();
}

$("encounter-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  form.classList.add("was-validated");
  if (!form.checkValidity()) return;
  setBusy($("encounter-save"), true);
  try {
    const result = await api.updateEncounter(ctx.patientId, ctx.encounterId, {
      start: fromIstInput($("encounter-start").value), end: fromIstInput($("encounter-end").value) });
    modal("encounter-modal").hide();
    ctx.onSaved(result);
  } catch (err) {
    $("encounter-error").textContent = err.message;
    $("encounter-error").hidden = false;
  } finally {
    setBusy($("encounter-save"), false);
  }
});
