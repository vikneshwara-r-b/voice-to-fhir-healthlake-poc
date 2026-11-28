// API client: adds the Cognito ID token, maps errors, sends users back to login on 401.
import { CONFIG } from "./config.js";
import { currentSession } from "./auth.js";

export class ApiError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function expired() {
  window.dispatchEvent(new Event("auth:expired"));
  return new ApiError(401, "UNAUTHORIZED", "Your session has expired. Sign in again.");
}

async function request(method, path, body, contentType = "application/json") {
  const session = currentSession();
  if (!session) throw expired();
  const headers = { Authorization: `Bearer ${session.idToken}` };
  if (body !== undefined) headers["Content-Type"] = contentType;
  let res;
  try {
    res = await fetch(`${CONFIG.apiUrl}${path}`, {
      method,
      headers,
      body: body === undefined || contentType !== "application/json" ? body : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK", "Could not reach the API. Check your connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) throw expired();
  if (!res.ok) throw new ApiError(res.status, data.error || `HTTP_${res.status}`, data.message || "Request failed.", data);
  return data;
}

const id = (draftId) => encodeURIComponent(draftId);

export const api = {
  patients: () => request("GET", "/patients"),
  patientChart: (patientId) => request("GET", `/patients/${encodeURIComponent(patientId)}/summary`),
  fhirView: (patientId, type) => request("GET", `/patients/${encodeURIComponent(patientId)}/fhir?type=${encodeURIComponent(type)}`),
  updateEncounter: (patientId, encounterId, period) =>
    request("PUT", `/patients/${encodeURIComponent(patientId)}/encounters/${encodeURIComponent(encounterId)}`, period),
  samples: () => request("GET", "/samples"),
  drafts: () => request("GET", "/drafts"),
  draft: (draftId) => request("GET", `/drafts/${id(draftId)}`),
  process: (patientId, sampleKey, options = {}) => request("POST", "/process", { patientId, sampleKey, ...options }),
  upload: (patientId, file, contentType, options = {}) =>
    request("POST", `/process/upload?${new URLSearchParams({ patientId, ...options })}`, file, contentType),
  generateNote: (draftId, template) => request("POST", `/drafts/${id(draftId)}/notes`, { template }),
  useNote: (draftId, template) => request("POST", `/drafts/${id(draftId)}/notes/${id(template)}/use`, {}),
  addItem: (draftId, fields) => request("POST", `/drafts/${id(draftId)}/items`, fields),
  editItem: (draftId, itemId, fields) => request("PUT", `/drafts/${id(draftId)}/items/${id(itemId)}`, fields),
  approve: (draftId, resourceIds, feedback, encounterStart) =>
    request("POST", `/drafts/${id(draftId)}/approve`, { resourceIds, feedback, encounterStart }),
  reject: (draftId, feedback) => request("POST", `/drafts/${id(draftId)}/reject`, { feedback }),
};
