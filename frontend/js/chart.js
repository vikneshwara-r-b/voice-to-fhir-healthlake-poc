// EHR-style patient chart: banner plus one card per section. All text via textContent (el()).
import { avatar, el, istDateTime } from "./views.js";

const $ = (id) => document.getElementById(id);
const badge = (text, kind) => el("span", { cls: `badge text-bg-${kind} me-1`, text });
const ORIGIN = { ai: ["AI-assisted", "info"], clinician: ["Clinician-entered", "primary"] };
const originBadge = (origin) => (ORIGIN[origin] ? badge(...ORIGIN[origin]) : null);
const words = (s) => (s ? s[0].toUpperCase() + s.slice(1).replace(/-/g, " ") : "–");
const statusBadge = (s) => (s ? badge(words(s), s === "active" ? "success" : "secondary") : null);
const td = (...children) => el("td", {}, ...children);
const sub = (text) => (text ? el("div", { cls: "small text-body-secondary", text }) : null);

function section(title, rows, columns, cells) {
  const body = rows.length
    ? el("div", { cls: "table-responsive" }, el("table", { cls: "table table-sm align-middle mb-0" },
      el("thead", {}, el("tr", {}, ...columns.map((c) => el("th", { text: c, attrs: { scope: "col" } })))),
      el("tbody", {}, ...rows.map((row) => el("tr", {}, ...cells(row).map((cell) =>
        cell instanceof Node ? cell : el("td", { cls: "text-nowrap", text: cell ?? "–" })))))))
    : el("p", { cls: "small text-body-secondary mb-0 p-2", text: "None recorded." });
  return el("div", { cls: "col" }, el("section", { cls: "card h-100", attrs: { "aria-label": title } },
    el("div", { cls: "card-header d-flex align-items-center" },
      el("h2", { cls: "h6 mb-0 me-auto", text: title }), badge(String(rows.length), "secondary")),
    el("div", { cls: "card-body p-1" }, body)));
}

export function renderBanner(p) {
  const sex = { female: "Female", male: "Male" }[p.gender] || "Unknown sex";
  $("chart-banner").replaceChildren(
    avatar(p.name, "avatar-lg"),
    el("div", { cls: "me-auto" }, el("h2", { cls: "h5 mb-0", text: p.name }),
      el("div", { cls: "small text-body-secondary", text: `${sex} · ${p.age ?? "?"} years · DOB ${p.birthDate || "unknown"}` })),
    el("div", { cls: "small text-body-secondary" }, el("span", { cls: "me-1", text: "Patient ID" }),
      el("code", { text: `${p.id.slice(0, 12)}…`, attrs: { title: p.id } })),
    el("button", { cls: "btn btn-outline-secondary btn-sm", text: "View FHIR record in HealthLake",
      attrs: { type: "button", "data-action": "fhir-view" } }));
}

export function renderChart(chart) {
  renderBanner(chart.patient);
  $("chart-sections").replaceChildren(
    section("Problem list", chart.problems, ["Problem", "Onset / recorded", "Status"], (r) => [
      td(el("span", { text: r.display }), sub(r.code && `${r.system} ${r.code}`), originBadge(r.origin)),
      r.date, td(statusBadge(r.clinicalStatus), r.verificationStatus ? badge(words(r.verificationStatus), "light border") : null)]),
    section("Medications", chart.medications, ["Medication", "Status", "Date"], (r) => [
      td(el("span", { text: r.display }), sub(`${r.kind}${r.code ? ` · ${r.system} ${r.code}` : ""}`), originBadge(r.origin)),
      td(statusBadge(r.status)), r.date]),
    section("Vital signs (latest)", chart.vitals, ["Measurement", "Value", "Date"], (r) => [
      td(el("span", { text: r.display }), originBadge(r.origin)), r.value, r.date]),
    section("Lab results (latest)", chart.labs, ["Test", "Result", "Date"], (r) => [
      td(el("span", { text: r.display }), originBadge(r.origin)), r.value, r.date]),
    section("Encounters", chart.encounters, ["Date and time", "Type", "Status"], (r) => [
      td(el("span", { cls: "text-nowrap", text: istDateTime(r.start) }), r.id ? el("button", {
        cls: "btn btn-link btn-sm p-0 d-block", text: "Edit date",
        attrs: { type: "button", "data-encounter-id": r.id, "data-start": r.start || "", "data-end": r.end || "",
          "data-type": r.type, "aria-label": `Edit date and time: ${r.type}, ${istDateTime(r.start)}` } }) : null),
      td(el("span", { text: r.type }), sub(r.class && words(r.class))), words(r.status)]),
    section("Clinical notes", chart.notes, ["Date", "Note", "Status"], (r) => [
      r.date, td(el("span", { text: r.title }), originBadge(r.origin)), words(r.status)]),
  );
}
