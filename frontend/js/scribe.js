// AWS HealthScribe views: clinical note with evidence links, and the engine comparison.
import { el } from "./views.js";

const $ = (id) => document.getElementById(id);
const badge = (text, kind) => el("span", { cls: `badge text-bg-${kind} me-1`, text });
const roleBadge = (role) => badge(role, role.startsWith("Clinician") ? "primary" : "success");
export const TEMPLATE_TITLES = {
  HISTORY_AND_PHYSICAL: "History and physical", PHYSICAL_SOAP: "SOAP (physical health)", DAP: "DAP",
  GIRPP: "GIRPP", BIRP: "BIRP", SIRP: "SIRP", BEHAVIORAL_SOAP: "SOAP (behavioral health)",
};

// Returns the template shown, so the caller can keep it selected across refreshes.
export function renderNotes(review, wanted, readOnly) {
  const notes = review.notes || {};
  const pending = review.draft.pendingNotes || [];
  const templates = Object.keys(notes);
  const current = [wanted, review.noteTemplate, templates[0]].find((t) => t && notes[t]) || null;
  $("note-select").replaceChildren(...templates.map((t) => el("option", { attrs: { value: t },
    text: `${notes[t].templateTitle}${t === review.noteTemplate ? " (proposed for HealthLake)" : ""}` })));
  $("note-select").value = current || "";
  $("note-select").disabled = !templates.length;
  $("btn-use-note").disabled = readOnly || !current || current === review.noteTemplate;
  $("btn-generate-note").disabled = readOnly;
  const error = review.draft.lastNoteError;
  $("note-status").textContent = pending.length
    ? `AWS HealthScribe is generating: ${pending.map((t) => TEMPLATE_TITLES[t] || t).join(", ")}. This page checks every 5 seconds.`
    : error ? `The last note request failed (${error}).` : "";
  $("note-evidence").textContent = "Select a sentence in the note.";
  if (!current) {
    $("note-sections").replaceChildren(el("p", { cls: "text-body-secondary",
      text: "No HealthScribe note yet. Choose a template and select Generate." }));
    return null;
  }
  $("note-sections").replaceChildren(...notes[current].sections.map((section, s) => el("section", { cls: "mb-3" },
    el("h3", { cls: "h6 mb-1", text: section.title }),
    el("ul", { cls: "list-unstyled mb-0" }, ...section.items.map((item, i) => el("li", {},
      el("button", { cls: "btn btn-link btn-sm text-start text-body p-0 note-sentence", text: item.text,
        attrs: { type: "button", "data-section": String(s), "data-item": String(i) } })))))));
  return current;
}

export function showEvidence(note, button) {
  document.querySelectorAll(".note-sentence.active").forEach((b) => b.classList.remove("active"));
  button.classList.add("active");
  const item = note.sections[Number(button.dataset.section)].items[Number(button.dataset.item)];
  $("note-evidence").replaceChildren(el("p", { cls: "fw-semibold", text: item.text }),
    ...(item.evidence.length ? item.evidence.map((e) => el("blockquote", { cls: "border-start border-3 ps-2 mb-2" },
      roleBadge(e.role), el("span", { text: e.text })))
      : [el("p", { cls: "text-body-secondary mb-0", text: "No linked dialogue." })]));
}

export function renderCompare(comparison) {
  $("tab-compare-item").hidden = !comparison;
  if (!comparison) return $("compare-columns").replaceChildren();
  const term = (dt, dd) => [el("dt", { cls: "col-sm-4", text: dt }), el("dd", { cls: "col-sm-8", text: dd })];
  $("compare-columns").replaceChildren(...["transcribe", "healthscribe"].filter((e) => comparison[e]).map((e) => {
    const c = comparison[e];
    return el("div", { cls: "col" }, el("div", { cls: "card h-100" },
      el("div", { cls: "card-header fw-semibold", text: c.label }),
      el("div", { cls: "card-body small" },
        el("dl", { cls: "row mb-2" }, ...term("Speaker roles", c.roleMethod || "–"),
          ...term("Turns / words", `${c.turns.length} / ${c.wordCount}`)),
        el("h3", { cls: "h6", text: "Conditions and medications found" }),
        c.proposals.length ? el("ul", { cls: "list-unstyled" }, ...c.proposals.map((p) => el("li", {},
          badge(p.resourceType === "Condition" ? "Condition" : "Medication", "light border"),
          el("code", { cls: "me-1", text: p.code }), el("span", { cls: "me-1", text: p.label }),
          p.flagged ? badge("Flagged", "warning") : null)))
          : el("p", { text: "None." }),
        c.insights ? el("h3", { cls: "h6", text: "HealthScribe clinical insights" }) : null,
        c.insights ? el("p", {}, ...c.insights.flatMap((i, n) => [n ? " " : null, badge(`${i.category}: ${i.text}`, "info")])) : null,
        el("h3", { cls: "h6", text: "Transcript" }),
        el("ol", { cls: "list-unstyled compare-transcript border rounded p-2 mb-0" },
          ...c.turns.map((t) => el("li", { cls: "mb-1" }, roleBadge(t.role), el("span", { text: t.text })))))));
  }));
}
