"""Encounter date/time updates: validation, patient scoping, metadata and Provenance."""

from datetime import datetime, timezone

import pytest

from shared.encounters import EncounterError, parse_instant, update_period
from shared.fhir_validate import validate
from shared.healthlake import HealthLakeError

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
USER = {"sub": "sub-1", "iss": "https://cognito-idp.us-east-1.amazonaws.com/pool", "name": "Demo Clinician"}


class FakeHealthLake:
    def __init__(self):
        self.store = {"enc-1": {"resourceType": "Encounter", "id": "enc-1", "status": "finished",
                                "class": {"code": "AMB"}, "subject": {"reference": "Patient/p-1"},
                                "period": {"start": "2026-11-27T07:46:24Z"},
                                "meta": {"versionId": "1", "lastUpdated": "2026-11-27T07:49:28Z",
                                         "security": [{"code": "AIAST", "system": "x"}]}}}
        self.created, self.version = [], 1

    def read(self, kind, rid):
        if rid not in self.store:
            raise HealthLakeError(404, ["not-found"])
        return dict(self.store[rid], meta=dict(self.store[rid]["meta"], versionId=str(self.version)))

    def put(self, resource):
        self.version += 1
        self.store[resource["id"]] = resource
        return f"Encounter/{resource['id']}"

    def create(self, resource):
        self.created.append(resource)
        return "Provenance/prov-1"


@pytest.mark.parametrize("value, ok", [
    ("2026-11-20T09:30:00.000Z", "2026-11-20T09:30:00Z"), ("2026-11-20T10:30:00+01:00", "2026-11-20T09:30:00Z"),
])
def test_parse_instant_normalises_to_utc(value, ok):
    assert parse_instant(value, "start", now=NOW) == ok


@pytest.mark.parametrize("value, code", [
    ("2026-11-20T09:30", "BAD_DATETIME"), ("yesterday", "BAD_DATETIME"), (123, "BAD_DATETIME"),
    ("1899-12-31T00:00:00Z", "DATETIME_OUT_OF_RANGE"), ("2026-12-30T00:00:00Z", "DATETIME_OUT_OF_RANGE"),
    (None, "MISSING_FIELD"),
])
def test_parse_instant_rejects(value, code):
    with pytest.raises(EncounterError) as exc:
        parse_instant(value, "start", now=NOW)
    assert exc.value.code == code


def test_update_sets_period_keeps_labels_and_writes_versioned_provenance():
    hl = FakeHealthLake()
    result = update_period(healthlake=hl, patient_id="p-1", encounter_id="enc-1", start="2026-11-20T09:30:00Z",
                           end="2026-11-20T09:50:00Z", user=USER, now="2026-11-27T12:00:00Z")
    stored = hl.store["enc-1"]
    assert stored["period"] == {"start": "2026-11-20T09:30:00Z", "end": "2026-11-20T09:50:00Z"}
    assert "versionId" not in stored["meta"] and "lastUpdated" not in stored["meta"]
    assert stored["meta"]["security"] == [{"code": "AIAST", "system": "x"}]
    assert [t["code"] for t in stored["meta"]["tag"]] == ["clinician-updated"]
    assert result["versionId"] == "2"
    [prov] = hl.created
    assert prov["target"] == [{"reference": "Encounter/enc-1/_history/2"}]
    assert prov["activity"]["coding"][0]["code"] == "UPDATE"
    assert prov["agent"][0]["who"]["identifier"]["value"] == "sub-1"
    assert validate(prov) == []


def test_update_is_scoped_to_the_patient_and_checks_order():
    hl = FakeHealthLake()
    with pytest.raises(EncounterError) as exc:
        update_period(healthlake=hl, patient_id="p-2", encounter_id="enc-1", start="2026-11-20T09:30:00Z",
                      end=None, user=USER, now="x")
    assert exc.value.status == 404 and hl.created == []
    with pytest.raises(EncounterError) as exc:
        update_period(healthlake=hl, patient_id="p-1", encounter_id="missing", start="2026-11-20T09:30:00Z",
                      end=None, user=USER, now="x")
    assert exc.value.status == 404
    with pytest.raises(EncounterError) as exc:
        update_period(healthlake=hl, patient_id="p-1", encounter_id="enc-1", start="2026-11-20T09:30:00Z",
                      end="2026-11-20T09:00:00Z", user=USER, now="x")
    assert exc.value.code == "END_BEFORE_START" and hl.version == 1
