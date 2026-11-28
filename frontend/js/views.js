// Views and rendering. All dynamic text goes through textContent (via el()); never innerHTML.
const VIEWS = ["login", "start", "drafts", "status", "review", "done"];
const $ = (id) => document.getElementById(id);

export function el(tag, { cls, text, attrs } = {}, ...children) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}
const badge = (text, kind = "secondary") => el("span", { cls: `badge text-bg-${kind} me-1`, text });
// A real space between badges, so copied text reads "A B" rather than "AB".
const spaced = (nodes) => nodes.flatMap((node, i) => (i ? [" ", node] : [node]));
const fmt = (n) => (typeof n === "number" ? n.toFixed(2) : "–");
// Initials badge for a person; the colour is picked from the name so it stays the same between visits.
export const initials = (name) => {
  const words = String(name || "").trim().split(/\s+/).filter(Boolean);
  return words.length ? (words[0][0] + (words.length > 1 ? words[words.length - 1][0] : "")).toUpperCase() : "?";
};
export function avatar(name, size = "") {
  const hue = [...String(name || "")].reduce((sum, c) => sum + c.charCodeAt(0), 0) % 5;
  return el("span", { cls: `avatar ${size} avatar-${hue}`.replace(/\s+/g, " "), text: initials(name), attrs: { "aria-hidden": "true" } });
}

const ENGINE_NAMES = { transcribe: "Amazon Transcribe Medical", healthscribe: "AWS HealthScribe" };
const ENGINE_SHORT = { transcribe: "Transcribe Medical", healthscribe: "HealthScribe", both: "Both engines" };
const FLAG_TEXT = {
  AI_GENERATED_NOTE: "AI-generated note, review carefully",
  HYPOTHETICAL: "Hypothetical", LOW_CONFIDENCE: "Low confidence", LOW_ENTITY_SCORE: "Low entity score",
  LOW_CODE_SCORE: "Low code score", TRANSCRIPT_OVER_10000_CHARS: "Transcript too long",
};
const EXCLUDE_TEXT = {
  NEGATION: "Negated", FAMILY_HISTORY: "Family history", NO_CODE: "No code found",
  NOT_DIAGNOSIS_SIGN_OR_SYMPTOM: "Not a diagnosis, sign or symptom",
};
const STATUS_LABEL = {
  UPLOADED: ["Uploaded", "secondary"], TRANSCRIBING: ["Transcribing", "secondary"], ANALYZING: ["Analysing", "secondary"],
  PENDING_REVIEW: ["Ready for review", "warning"], APPROVED: ["Writing", "info"], WRITTEN: ["Written", "success"],
  REJECTED: ["Rejected", "dark"], FAILED: ["Failed", "danger"],
};
const ACTION_LABEL = { PENDING_REVIEW: "Review", WRITTEN: "View result", APPROVED: "Resume", FAILED: "View", REJECTED: "View" };
const statusBadge = (s) => badge(...(STATUS_LABEL[s] || [s, "secondary"]));

export const STEPS = [
  ["UPLOADED", "Uploaded", 10], ["TRANSCRIBING", "Transcribing", 35], ["ANALYZING", "Analysing", 65],
  ["PENDING_REVIEW", "Ready for review", 90], ["WRITTEN", "Written", 100],
];

// --- generic -------------------------------------------------------------------------------
export function showView(name) {
  for (const view of VIEWS) $(`view-${view}`).hidden = view !== name;
  $(`view-${name}`).querySelector("h1")?.focus();
}

export function setSignedInUser(session) {
  $("nav-user").hidden = !session;
  $("nav-links").hidden = !session;
  $("nav-avatar").textContent = session ? initials(session.displayName || session.username) : "";
  $("nav-username").textContent = session ? session.displayName || session.username : "";
  $("nav-username").title = session ? `Cognito username: ${session.username}` : "";
}

export function showAlert(kind, message) {
  const close = el("button", { cls: "btn-close", attrs: { type: "button", "data-bs-dismiss": "alert", "aria-label": "Close" } });
  $("alerts").append(el("div", { cls: `alert alert-${kind} alert-dismissible`, attrs: { role: kind === "danger" ? "alert" : "status" } },
    el("span", { text: message }), close));
}

export const clearAlerts = () => $("alerts").replaceChildren();

export function setLoginError(message) {
  $("login-error").textContent = message || "";
  $("login-error").hidden = !message;
}

export function setBusy(button, busy) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  button.querySelector(".spinner-border").hidden = !busy;
}

export function loading(container, label) {
  container.replaceChildren(el("div", { cls: "d-flex align-items-center gap-2 p-2 text-body-secondary" },
    el("span", { cls: "spinner-border spinner-border-sm", attrs: { "aria-hidden": "true" } }), el("span", { text: label })));
}

// feedback: undefined (none), or { label, required } for a feedback textarea. Resolves { ok, feedback }.
export function confirmAction({ title, body, okLabel, okClass, feedback }) {
  $("confirm-title").textContent = title;
  $("confirm-body").textContent = body;
  const ok = $("confirm-ok");
  ok.textContent = okLabel;
  ok.className = `btn ${okClass}`;
  const text = $("confirm-feedback");
  $("confirm-feedback-wrap").hidden = !feedback;
  $("confirm-feedback-label").textContent = feedback?.label || "";
  text.value = "";
  text.required = Boolean(feedback?.required);
  text.classList.remove("is-invalid");
  const modal = window.bootstrap.Modal.getOrCreateInstance($("confirm-modal"));
  return new Promise((resolve) => {
    let confirmed = false;
    ok.onclick = () => {
      if (feedback?.required && text.value.trim().length < 5) {
        text.classList.add("is-invalid");
        return text.focus();
      }
      confirmed = true;
      modal.hide();
    };
    $("confirm-modal").addEventListener("hidden.bs.modal", () => resolve({ ok: confirmed, feedback: text.value.trim() }), { once: true });
    modal.show();
  });
}

// Every time is shown and entered in India Standard Time; the API and HealthLake keep ISO 8601 UTC.
export const TIME_ZONE = "Asia/Kolkata";
const IST_OFFSET = "+05:30"; // IST has no daylight saving, so the offset is fixed
const IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000;
const istFormat = new Intl.DateTimeFormat("en-IN", { timeZone: TIME_ZONE, dateStyle: "medium", timeStyle: "short" });
const validDate = (iso) => (iso && !Number.isNaN(new Date(iso).getTime()) ? new Date(iso) : null);

export const istDateTime = (iso) => (validDate(iso) ? `${istFormat.format(validDate(iso))} IST` : "–");
// <input type="datetime-local"> has no zone: its "YYYY-MM-DDTHH:mm" value is read and written as IST.
export function toIstInput(iso) {
  const d = validDate(iso);
  return d ? new Date(d.getTime() + IST_OFFSET_MS).toISOString().slice(0, 16) : "";
}
export function fromIstInput(value) {
  if (!value) return undefined;
  const d = new Date(`${value.length === 16 ? `${value}:00` : value}${IST_OFFSET}`);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

export function setAudio(player, url) {
  player.pause();
  if (url) player.src = url;
  else player.removeAttribute("src");
  player.hidden = !url;
}

// --- start ---------------------------------------------------------------------------------
export function renderPatients(patients) {
  $("patient-list").replaceChildren(...patients.map((p) => {
    const sex = { female: "Female", male: "Male" }[p.gender] || "Unknown sex";
    const count = p.activeConditions.length;
    // The radio stays focusable and announced; the row (initials, highlight, focus ring) shows its state.
    return el("label", { cls: "list-group-item list-group-item-action patient-option d-flex gap-3 align-items-center py-3" },
      el("input", { cls: "visually-hidden", attrs: { type: "radio", name: "patient", value: p.id } }),
      avatar(p.name),
      el("span", { cls: "flex-grow-1 min-w-0" }, el("span", { cls: "fw-semibold d-block text-truncate", text: p.name }),
        el("span", { cls: "small text-body-secondary", text: `${sex} · ${p.age ?? "?"} y · DOB ${p.birthDate || "?"}` })),
      el("span", { cls: `badge border flex-shrink-0 ${count ? "bg-warning-subtle text-warning-emphasis border-warning-subtle" : "text-bg-light"}`,
        text: `${count} active`, attrs: { title: `${count} active problem${count === 1 ? "" : "s"}` } }));
  }));
}

export function renderSamples(samples) {
  $("sample-select").replaceChildren(el("option", { text: "Choose a sample…", attrs: { value: "" } }),
    ...samples.map((s) => el("option", { attrs: { value: s.key, "data-url": s.url || "" },
      text: s.name.includes("-stereo") ? `${s.name} (stereo: clinician left, patient right)` : s.name })));
}

export function setActiveNav(name) {
  for (const link of ["start", "drafts"]) {
    const node = $(`nav-${link}`);
    node.classList.toggle("active", link === name);
    if (link === name) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  }
}

// --- drafts --------------------------------------------------------------------------------
export function renderDrafts(drafts, patientName) {
  if (!drafts.length) {
    return $("drafts-body").replaceChildren(el("tr", {}, el("td", { cls: "text-body-secondary", text: "No drafts yet. Start a new consultation.", attrs: { colspan: "5" } })));
  }
  $("drafts-body").replaceChildren(...drafts.map((d) => {
    const name = patientName(d.patientId) || (d.patientId ? `Patient ${d.patientId.slice(0, 8)}` : "–");
    const counts = `${ENGINE_SHORT[d.engine] || "Transcribe Medical"}${d.proposedCount !== undefined
      ? ` · ${d.proposedCount} proposed · ${d.flaggedCount} flagged · ${d.excludedCount} excluded` : ""}`;
    const label = ACTION_LABEL[d.status] || "View progress";
    return el("tr", {},
      el("td", { cls: "text-nowrap", text: istDateTime(d.createdAt) }),
      el("td", {}, el("span", { cls: "d-inline-flex align-items-center gap-2" }, avatar(name, "avatar-sm"), el("span", { text: name }))),
      el("td", {}, statusBadge(d.status), d.errorCode ? el("span", { cls: "small text-body-secondary", text: d.errorCode }) : null),
      el("td", { cls: "small", text: counts }),
      el("td", { cls: "text-end" }, el("a", { cls: "btn btn-sm btn-outline-primary text-nowrap", text: label,
        attrs: { href: `#/drafts/${encodeURIComponent(d.draftId)}`, "aria-label": `${label}: ${name}, ${istDateTime(d.createdAt)}` } })));
  }));
}

// --- status --------------------------------------------------------------------------------
export function renderEngines(draft) {
  const engines = draft.engines || (draft.engine === "both" ? ["transcribe", "healthscribe"] : [draft.engine || "transcribe"]);
  const pending = draft.pendingEngines || [];
  const early = ["UPLOADED", "TRANSCRIBING"].includes(draft.status);
  $("status-engines").textContent = `AI engine${engines.length > 1 ? "s" : ""}: ${engines.map((e) =>
    `${ENGINE_NAMES[e]} (${pending.includes(e) || (early && !pending.length) ? "working" : "done"})`).join(" · ")}`;
}

export function renderStatus(draftId, status, errorCode) {
  $("status-draft-id").textContent = draftId;
  const failed = status === "FAILED";
  const current = STEPS.findIndex(([s]) => s === (status === "APPROVED" ? "WRITTEN" : status));
  const pct = current >= 0 ? STEPS[current][2] : 0;
  $("status-bar").style.width = `${pct}%`;
  $("status-bar").className = `progress-bar${failed ? " bg-danger" : ""}`;
  $("status-progress").setAttribute("aria-valuenow", String(pct));
  if (current >= 0 || !failed) {
    $("status-steps").replaceChildren(...STEPS.map(([, label], i) => {
      const [text, kind] = i < current || pct === 100 ? ["Done", "success"] : i === current ? ["In progress", "primary"] : ["Waiting", "secondary"];
      return el("li", { cls: "list-group-item d-flex justify-content-between align-items-center" }, el("span", { text: label }), badge(text, kind));
    }));
  }
  $("status-failed").hidden = !failed;
  if (failed) $("status-failed").textContent = `Processing failed (${errorCode || "unknown error"}). Go back to start and process the sample again.`;
  $("status-live").textContent = `Status: ${failed ? "failed" : STEPS[current]?.[1] || status}`;
}

// --- review --------------------------------------------------------------------------------
export function renderReview(data, readOnly, patientName) {
  const { draft, turns, entities, proposed, excluded, thresholds } = data;
  setAudio($("review-audio"), data.audioUrl);
  $("review-audio-wrap").hidden = !data.audioUrl;
  const engine = data.primaryEngine === "healthscribe" ? "AWS HealthScribe" : "Amazon Transcribe Medical";
  const chip = (text) => el("span", { cls: "meta-chip", text });
  $("review-meta").replaceChildren(...spaced([
    el("span", { cls: "meta-chip fw-semibold" }, avatar(patientName || "Patient", "avatar-sm me-2"), el("span", { text: patientName || "Patient" })),
    chip(engine), chip(`${turns.length} turns`), chip(`${proposed.length} proposed`), chip(`${excluded.length} excluded`),
    chip(`Draft ${draft.draftId}`),
    el("span", { cls: "small text-body-secondary", text: `Flags below entity score ${thresholds.confidence} or code score ${thresholds.codeScoreFloor}` }),
  ]));
  $("transcript-source").textContent = `${data.primaryEngine === "healthscribe"
    ? "Generated by AWS HealthScribe; each turn shows the section HealthScribe classified it under."
    : "Generated by Amazon Transcribe Medical (batch job, primary care, conversation)."} Speaker roles: ${data.roleMethod || "first speaker labelled Clinician"}.`;

  $("transcript-list").replaceChildren(...turns.map((t) => {
    const sections = [...new Set((t.segments || []).map((g) => g.section).filter((x) => x && x !== "OTHER"))];
    const clinician = t.role.startsWith("Clinician");
    return el("li", { cls: "d-flex gap-2 mb-2 align-items-start" },
      el("span", { cls: "turn-role pt-1" }, badge(t.role, clinician ? "primary" : "success")),
      el("div", { cls: `turn-bubble ${clinician ? "clinician" : "patient"}` }, el("span", { text: t.text }),
        ...sections.map((x) => el("span", { cls: "badge text-bg-light border ms-1", text: x.replace(/_/g, " ").toLowerCase() }))));
  }));

  $("entities-body").replaceChildren(...entities.map((e) => el("tr", {},
    el("td", { text: e.text }), el("td", { cls: "small", text: e.category }),
    el("td", {}, ...spaced(e.traits.map((t) => badge(`${t.name} ${fmt(t.score)}`,
      t.name === "NEGATION" || t.name === "PERTAINS_TO_FAMILY" ? "dark" : "info")))),
    el("td", { text: fmt(e.score) }),
    el("td", { cls: "small", text: e.code ? `${e.codeSystem} ${e.code} (${fmt(e.codeScore)})` : "–", attrs: e.codeDescription ? { title: e.codeDescription } : {} }))));

  renderResources(proposed, readOnly);
  $("excluded-list").replaceChildren(...(excluded.length ? excluded.map((e) => el("li", { cls: "list-group-item" },
    badge(EXCLUDE_TEXT[e.reason] || e.reason, "dark"), el("span", { text: e.text }),
    e.mentions > 1 ? el("span", { cls: "text-body-secondary", text: ` (${e.mentions} mentions)` }) : null))
    : [el("li", { cls: "list-group-item text-body-secondary", text: "Nothing excluded." })]));

  for (const id of ["btn-approve", "btn-reject"]) $(id).disabled = readOnly;
}

// selected: optional Set of ids to keep ticked when re-rendering after an edit.
export function renderResources(proposed, readOnly, selected) {
  $("resource-actions").hidden = readOnly;
  $("resource-list").replaceChildren(...proposed.map((p) => {
    const box = el("input", { cls: "form-check-input", attrs: { type: "checkbox", id: `res-${p.id}`, value: p.id } });
    box.checked = selected ? selected.has(p.id) : p.selected;
    box.disabled = readOnly;
    const action = (text, attr) => el("button", { cls: "btn btn-link btn-sm p-0 me-2", text, attrs: { type: "button", [attr]: p.id, "aria-label": `${text}: ${p.label}` } });
    const detail = p.origin === "clinician" ? "Added by you" : [p.sourceText ? `Heard as “${p.sourceText}”` : null,
      p.confidence !== null && p.confidence !== undefined ? `confidence ${fmt(p.confidence)}` : null, p.codeScore !== null && p.codeScore !== undefined ? `code score ${fmt(p.codeScore)}` : null].filter(Boolean).join(" · ");
    const marks = [p.origin === "clinician" ? badge("Added by clinician", "primary") : null, p.edited ? badge("Edited", "info") : null,
      ...p.flagReasons.map((r) => badge(`Flagged: ${FLAG_TEXT[r] || r}`, "warning"))].filter(Boolean);
    return el("div", { cls: "list-group-item" },
      el("div", { cls: "form-check" }, box, el("label", { cls: "form-check-label", attrs: { for: `res-${p.id}` } },
        el("span", { cls: "fw-semibold me-2", text: p.label }), badge(p.resourceType, "light border"), el("code", { text: p.code }))),
      el("div", { cls: "small text-body-secondary ms-4" }, el("span", { cls: "me-2", text: detail }), ...spaced(marks), " ",
        readOnly ? null : action("Edit", "data-edit"), action("View JSON", "data-json")));
  }));
  $("json-select").replaceChildren(...proposed.map((p) => el("option", { text: `${p.resourceType}: ${p.label}`, attrs: { value: p.id } })));
  showJson(proposed, proposed[0]?.id);
}

export function showJson(proposed, id) {
  const item = proposed.find((p) => p.id === id);
  $("json-select").value = id || "";
  $("json-view").textContent = item ? JSON.stringify(item.resource, null, 2) : "";
}

export function selectedResourceIds() {
  return [...document.querySelectorAll("#resource-list input[type=checkbox]:checked")].map((b) => b.value);
}

// --- done ----------------------------------------------------------------------------------
export function renderDone(written) {
  $("done-list").replaceChildren(...written.map((w) =>
    el("li", { cls: "list-group-item d-flex justify-content-between gap-2" },
      el("span", { cls: "fw-semibold", text: w.resourceType }), el("code", { cls: "text-break", text: w.id }))));
}
