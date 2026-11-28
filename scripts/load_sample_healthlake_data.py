#!/usr/bin/env python3
"""Load the 3 synthetic demo patients (and their history) into a HealthLake FHIR R4 datastore.

Adapted from the reference repo's loader. Differences: stdlib urllib + botocore
SigV4 (shared with the Lambdas), deterministic ids with PUT (re-runs update in
place), and a pre-check that refuses to add duplicates next to data loaded by the
reference repo's POST-based loader.

Usage:
    python3 scripts/load_sample_healthlake_data.py --datastore-id <id> [--region us-east-1] \
        [--profile NAME] [--dry-run] [--force]
"""

import argparse
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambdas"))
sys.path.insert(0, str(ROOT / "scripts"))

from sample_fhir_data import PATIENTS, all_resources  # noqa: E402
from shared.healthlake import HealthLakeClient, HealthLakeError  # noqa: E402

CHECKED_TYPES = ("Condition", "Observation", "Encounter", "MedicationRequest")


def foreign_resources(client: HealthLakeClient, patient: dict) -> int:
    """Count resources for this patient that this loader did not create (no v2f- id)."""
    count = 0
    for resource_type in CHECKED_TYPES:
        found = client.search(resource_type, {"subject": f"Patient/{patient['id']}", "_count": "100"})
        count += sum(1 for r in found if not r.get("id", "").startswith("v2f-"))
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--datastore-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    parser.add_argument("--force", action="store_true", help="load even if other data exists for these patients")
    args = parser.parse_args()

    if args.dry_run:
        for p in PATIENTS:
            counts: dict[str, int] = {}
            for r in all_resources(p):
                counts[r["resourceType"]] = counts.get(r["resourceType"], 0) + 1
            print(f"{p['given']} {p['family']} ({p['id'][:8]}): {counts}")
        return 0

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = HealthLakeClient(args.datastore_id, args.region, session.get_credentials())

    if not args.force:
        existing = sum(foreign_resources(client, p) for p in PATIENTS)
        if existing:
            print(f"Found {existing} resources for the demo patients that this loader did not create "
                  "(probably loaded by the reference repo). Nothing written. Use --force to load anyway.")
            return 2

    for p in PATIENTS:
        resources = all_resources(p)
        for resource in resources:
            try:
                client.put(resource)
            except HealthLakeError as exc:
                print(f"  FAILED {resource['resourceType']}/{resource['id']}: {exc}")
                return 1
        print(f"{p['given']} {p['family']} ({p['id'][:8]}): {len(resources)} resources written")

    print(f"Done. {len(PATIENTS)} synthetic patients loaded into {args.datastore_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
