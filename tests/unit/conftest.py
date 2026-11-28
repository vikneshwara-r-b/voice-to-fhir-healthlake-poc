"""Shared helpers for Lambda unit tests. All data is synthetic."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambdas"))

_TOKEN = re.compile(r"[\w'-]+|[.,?!]")


def transcribe_output(turns: list[tuple[str, str]], confidence: str = "0.99") -> dict:
    """Build Transcribe Medical batch output (speaker labels on) for (speaker_label, text) turns."""
    items, segments, t = [], [], 0.0
    for speaker, text in turns:
        seg_items, seg_start = [], t
        for token in _TOKEN.findall(text):
            if token in ".,?!":
                items.append({"type": "punctuation", "alternatives": [{"confidence": "0.0", "content": token}]})
                continue
            start, end = f"{t:.3f}", f"{t + 0.3:.3f}"
            t += 0.35
            items.append({"type": "pronunciation", "start_time": start, "end_time": end,
                          "alternatives": [{"confidence": confidence, "content": token}]})
            seg_items.append({"start_time": start, "end_time": end, "speaker_label": speaker})
        segments.append({"start_time": f"{seg_start:.3f}", "end_time": f"{t:.3f}",
                         "speaker_label": speaker, "items": seg_items})
        t += 0.5
    return {
        "jobName": "test-job",
        "accountId": "111111111111",
        "status": "COMPLETED",
        "results": {
            "transcripts": [{"transcript": " ".join(text for _, text in turns)}],
            "speaker_labels": {"speakers": len({s for s, _ in turns}), "segments": segments},
            "items": items,
        },
    }


def cm_entity(text: str, category: str, *, traits=(), score=0.95, codes=(), begin=0,
              entity_type=None, attributes=()) -> dict:
    """A Comprehend Medical entity. `codes` = [(code, description, score)]."""
    entity = {
        "Id": 0,
        "Text": text,
        "Category": category,
        "Type": entity_type or ("DX_NAME" if category == "MEDICAL_CONDITION" else "GENERIC_NAME"),
        "Score": score,
        "BeginOffset": begin,
        "EndOffset": begin + len(text),
        "Traits": [{"Name": name, "Score": 0.9} for name in traits],
        "Attributes": list(attributes),
    }
    concept_key = "ICD10CMConcepts" if category == "MEDICAL_CONDITION" else "RxNormConcepts"
    entity[concept_key] = [{"Code": c, "Description": d, "Score": s} for c, d, s in codes]
    return entity


@pytest.fixture
def lambda_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DRAFTS_TABLE", "test-drafts")
    monkeypatch.setenv("JOB_PREFIX", "voice-to-fhir-test")
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.80")
