#!/usr/bin/env python3
"""Smoke-test the deployed API as the demo clinician (M3).

Signs in with Cognito InitiateAuth (USER_PASSWORD_AUTH, like the web UI), then calls
GET /patients, GET /samples and GET /drafts. With --process it also starts a draft
from a sample and polls GET /drafts/{id} until it is ready for review.

The password is read with getpass (or V2F_DEMO_PASSWORD), never from the command line.

Usage:
    python3 scripts/api_smoke.py --email clinician.demo@example.com \
        [--process consult-02-gout-follow-up.wav --patient-prefix 69834515] \
        [--stack VoiceToFhir-dev] [--profile NAME] [--region us-east-1]
"""

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

import boto3


def api_call(base: str, token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--process", metavar="SAMPLE", help="sample file name to run, e.g. consult-02-gout-follow-up.wav")
    parser.add_argument("--patient-prefix", default="", help="patient id prefix for --process (default: first patient)")
    parser.add_argument("--stack", default="VoiceToFhir-dev")
    parser.add_argument("--profile")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    stack = session.client("cloudformation").describe_stacks(StackName=args.stack)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    base = outputs["ApiUrl"].rstrip("/")

    status, _ = api_call(base, "not-a-token", "GET", "/patients")
    print(f"GET /patients without a valid token -> {status} (expect 401)")

    password = os.environ.get("V2F_DEMO_PASSWORD") or getpass.getpass("Demo user password: ")
    auth = session.client("cognito-idp").initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH", ClientId=outputs["UserPoolClientId"],
        AuthParameters={"USERNAME": args.email, "PASSWORD": password})
    token = auth["AuthenticationResult"]["IdToken"]

    status, body = api_call(base, token, "GET", "/patients")
    print(f"GET /patients -> {status}")
    for p in body.get("patients", []):
        print(f"  {p['id'][:8]}  {p['name']:<18} {p['age']} {p['gender']:<6} {len(p['activeConditions'])} active conditions")
    status, body = api_call(base, token, "GET", "/samples")
    print(f"GET /samples -> {status}: {[s['name'] for s in body.get('samples', [])]}")
    status, body = api_call(base, token, "GET", "/drafts")
    print(f"GET /drafts -> {status}: {len(body.get('drafts', []))} drafts")

    if not args.process:
        return 0
    patients = api_call(base, token, "GET", "/patients")[1]["patients"]
    patient = next((p for p in patients if p["id"].startswith(args.patient_prefix)), None)
    if patient is None:
        print(f"No patient id starts with {args.patient_prefix!r}")
        return 1
    status, body = api_call(base, token, "POST", "/process",
                            {"patientId": patient["id"], "sampleKey": f"samples/{args.process}"})
    print(f"POST /process ({patient['name']}) -> {status}: {body}")
    if status != 202:
        return 1
    draft_id, last = body["draftId"], None
    for _ in range(120):
        draft = api_call(base, token, "GET", f"/drafts/{draft_id}")[1]
        current = draft["draft"]["status"]
        if current != last:
            print(f"  {current}")
            last = current
        if current in ("PENDING_REVIEW", "FAILED"):
            break
        time.sleep(5)
    if last == "PENDING_REVIEW":
        print(f"  {len(draft['turns'])} turns, {len(draft['entities'])} entities, "
              f"{len(draft['proposed'])} proposed ({sum(p['selected'] for p in draft['proposed'])} pre-selected), "
              f"{len(draft['excluded'])} excluded")
    return 0 if last == "PENDING_REVIEW" else 1


if __name__ == "__main__":
    sys.exit(main())
