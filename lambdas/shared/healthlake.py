"""Minimal HealthLake FHIR R4 REST client: SigV4 (service "healthlake") + urllib.

Endpoint: https://healthlake.{region}.amazonaws.com/datastore/{datastoreId}/r4/{ResourceType}
Errors carry the HTTP status and OperationOutcome issue codes only, never bodies
(which could echo resource content).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

FHIR_JSON = "application/fhir+json"


class HealthLakeError(Exception):
    """status 0 = network error or timeout."""

    def __init__(self, status: int, issue_codes: list[str]):
        super().__init__(f"HealthLake HTTP {status} {','.join(issue_codes) or ''}".strip())
        self.status = status
        self.issue_codes = issue_codes

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status == 429 or self.status >= 500


def _issue_codes(raw: bytes) -> list[str]:
    try:
        body = json.loads(raw)
    except ValueError:
        return []
    return [i.get("code", "") for i in body.get("issue", []) if isinstance(i, dict)][:5]


class HealthLakeClient:
    def __init__(self, datastore_id: str, region: str, credentials, timeout: float = 20.0):
        """`credentials` is a botocore Credentials object (refreshable credentials are fine)."""
        self.region = region
        self.base_url = f"https://healthlake.{region}.amazonaws.com/datastore/{datastore_id}/r4/"
        self._credentials = credentials
        self._timeout = timeout

    def request(self, method: str, path: str = "", *, body: dict | None = None,
                params: dict | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        all_headers = {"Accept": FHIR_JSON, **(headers or {})}
        if data is not None:
            all_headers["Content-Type"] = FHIR_JSON

        signed = AWSRequest(method=method, url=url, data=data, headers=all_headers)
        SigV4Auth(self._credentials.get_frozen_credentials(), "healthlake", self.region).add_auth(signed)
        request = urllib.request.Request(url, data=data, headers=dict(signed.headers.items()), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {}), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            raise HealthLakeError(exc.code, _issue_codes(exc.read())) from None
        except (urllib.error.URLError, TimeoutError):
            raise HealthLakeError(0, ["network"]) from None

    def create(self, resource: dict) -> str:
        """POST a resource; return its server-assigned reference, e.g. "Condition/abc"."""
        kind = resource["resourceType"]
        _, body, headers = self.request("POST", kind, body=resource)
        new_id = body.get("id")
        if not new_id:  # fall back to Location: .../r4/{Type}/{id}/_history/1
            location = headers.get("Location") or headers.get("location") or ""
            parts = location.split("/r4/", 1)[-1].split("/")
            new_id = parts[1] if len(parts) > 1 and parts[0] == kind else None
        if not new_id:
            raise HealthLakeError(0, ["no-id-in-response"])
        return f"{kind}/{new_id}"

    def put(self, resource: dict) -> str:
        """PUT (create or update) a resource with a client-assigned id; return its reference."""
        self.request("PUT", f"{resource['resourceType']}/{resource['id']}", body=resource)
        return f"{resource['resourceType']}/{resource['id']}"

    def read(self, resource_type: str, resource_id: str) -> dict:
        return self.request("GET", f"{resource_type}/{resource_id}")[1]

    def search(self, resource_type: str, params: dict) -> list[dict]:
        bundle = self.request("GET", resource_type, params=params)[1]
        return [e["resource"] for e in bundle.get("entry", []) if "resource" in e]
