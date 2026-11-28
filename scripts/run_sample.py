#!/usr/bin/env python3
"""Run one bundled sample through the pipeline without the UI (M2 test driver).

Does what POST /process will do: creates the draft record (UPLOADED), copies
samples/<file> to audio/<patientId>/<encounterId>/consult.wav (which triggers the
pipeline), then polls the status every 5 s and prints a summary of the draft.

Usage:
    python3 scripts/run_sample.py --sample consult-01-knee-pain-blood-pressure.wav \
        --patient-id <id> [--stack VoiceToFhir-dev] [--profile NAME] [--region us-east-1]
"""

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3

FINAL = {"PENDING_REVIEW", "FAILED", "WRITTEN", "REJECTED"}


def stack_outputs(session, stack: str) -> dict:
    described = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", required=True, help="file name under samples/ in the data bucket")
    parser.add_argument("--patient-id", required=True)
    parser.add_argument("--stack", default="VoiceToFhir-dev")
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    outputs = stack_outputs(session, args.stack)
    bucket, table_name = outputs["DataBucketName"], outputs["DraftsTableName"]
    s3, table = session.client("s3"), session.resource("dynamodb").Table(table_name)

    encounter_id = str(uuid.uuid4())
    audio_key = f"audio/{args.patient_id}/{encounter_id}/consult.wav"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    table.put_item(
        Item={"draftId": encounter_id, "status": "UPLOADED", "patientId": args.patient_id,
              "encounterId": encounter_id, "audioKey": audio_key, "createdAt": now, "updatedAt": now},
        ConditionExpression="attribute_not_exists(draftId)",
    )
    s3.copy_object(Bucket=bucket, Key=audio_key, CopySource={"Bucket": bucket, "Key": f"samples/{args.sample}"})
    print(f"draftId {encounter_id}: copied samples/{args.sample} -> {audio_key}")

    started, last = time.monotonic(), None
    while time.monotonic() - started < args.timeout:
        item = table.get_item(Key={"draftId": encounter_id}, ConsistentRead=True).get("Item", {})
        status = item.get("status")
        if status != last:
            print(f"  [{time.monotonic() - started:5.0f} s] {status}" + (f" ({item['errorCode']})" if "errorCode" in item else ""))
            last = status
        if status in FINAL:
            break
        time.sleep(5)
    else:
        print("Timed out waiting for the pipeline.")
        return 1

    if last != "PENDING_REVIEW":
        return 1
    draft = json.loads(s3.get_object(Bucket=bucket, Key=f"drafts/{encounter_id}.json")["Body"].read())
    print(f"\n{len(draft['turns'])} turns; proposed resources:")
    for p in draft["proposed"]:
        flag = f"  FLAGGED {','.join(p['flagReasons'])}" if p["flagged"] else ""
        print(f"  [{'x' if p['selected'] else ' '}] {p['id']:<24} {p['code']:<10} {p['label']}{flag}")
    print("excluded:")
    for e in draft["excluded"]:
        print(f"  - {e['source']:<8} {e['reason']:<30} {e['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
