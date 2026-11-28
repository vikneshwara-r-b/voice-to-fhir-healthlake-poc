// Add or edit a proposed resource. Sends named fields only; the API builds and validates the FHIR.
import { api } from "./api.js";
import { setBusy } from "./views.js";

const $ = (id) => document.getElementById(id);
const FIELDS = {
  Condition: ["code", "display", "clinicalStatus", "verificationStatus"],
  MedicationStatement: ["code", "display", "status", "dosage"],
  DocumentReference: ["transcript"],
};
const INPUTS = { code: "edit-code", display: "edit-display", clinicalStatus: "edit-clinical",
  verificationStatus: "edit-verification", status: "edit-status", dosage: "edit-dosage", transcript: "edit-transcript" };
const REQUIRED = new Set(["code", "display", "transcript"]);
const CODES = {
  Condition: ["ICD-10-CM code", "For example I10 or M25.561.", "[A-TV-Za-tv-z][0-9][0-9A-Za-z](\\.[0-9A-Za-z]{1,4})?"],
  MedicationStatement: ["RxNorm code", "Digits only, for example 197361.", "[0-9]{1,10}"],
};
let ctx = null;

function decode(data) {
  return new TextDecoder().decode(Uint8Array.from(atob(data || ""), (c) => c.charCodeAt(0)));
}

function valuesOf(item) {
  const r = item.resource;
  if (item.resourceType === "DocumentReference") return { transcript: decode(r.content[0].attachment.data) };
  const concept = item.resourceType === "Condition" ? r.code : r.medicationCodeableConcept;
  const coding = concept.coding[0];
  return {
    code: coding.code, display: coding.display || concept.text,
    clinicalStatus: r.clinicalStatus?.coding[0].code, verificationStatus: r.verificationStatus?.coding[0].code,
    status: r.status, dosage: r.dosage?.[0].text || "",
  };
}

export function openEditor({ draftId, item, kind, onSaved }) {
  const type = item ? item.resourceType : kind;
  ctx = { draftId, item, type, onSaved };
  $("edit-title").textContent = item ? `Edit ${item.label}` : `Add ${type === "Condition" ? "a condition" : "a medication"}`;
  const values = item ? valuesOf(item) : { clinicalStatus: "active", verificationStatus: "confirmed", status: "active" };
  for (const [field, inputId] of Object.entries(INPUTS)) {
    const visible = FIELDS[type].includes(field);
    $(inputId).closest("[data-field]").hidden = !visible;
    $(inputId).required = visible && REQUIRED.has(field);
    $(inputId).value = values[field] ?? "";
  }
  if (CODES[type]) {
    const [label, help, pattern] = CODES[type];
    $("edit-code-label").textContent = label;
    $("edit-code-help").textContent = help;
    $("edit-code").pattern = pattern;
  }
  $("edit-error").hidden = true;
  $("edit-form").classList.remove("was-validated");
  window.bootstrap.Modal.getOrCreateInstance($("edit-modal")).show();
}

$("edit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  form.classList.add("was-validated");
  if (!form.checkValidity()) return;
  const fields = Object.fromEntries(FIELDS[ctx.type].map((f) => [f, $(INPUTS[f]).value]));
  if (!ctx.item) fields.resourceType = ctx.type;
  setBusy($("edit-save"), true);
  try {
    const { item } = ctx.item ? await api.editItem(ctx.draftId, ctx.item.id, fields) : await api.addItem(ctx.draftId, fields);
    window.bootstrap.Modal.getOrCreateInstance($("edit-modal")).hide();
    ctx.onSaved(item, !ctx.item);
  } catch (err) {
    $("edit-error").textContent = [err.message, ...(err.details?.problems || [])].join(" ");
    $("edit-error").hidden = false;
  } finally {
    setBusy($("edit-save"), false);
  }
});
