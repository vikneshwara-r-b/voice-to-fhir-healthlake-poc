"""Run Comprehend Medical over the transcript, chunked by speaker turn.

Limits (AWS docs, Nov 2026): DetectEntitiesV2 20,000 bytes; InferICD10CM and
InferRxNorm 10,000 characters. Chunks are capped at 9,000 UTF-8 bytes, which is
safe for every call under either reading. Entity offsets are rebased onto the
full document (turn texts joined with "\n") and tagged with their turn index.
"""

import bisect
import copy

MAX_CHUNK_BYTES = 9000


def build_document(turns: list[dict]) -> tuple[str, list[int]]:
    """Join turn texts with newlines; return the text and each turn's start offset."""
    starts, offset = [], 0
    for turn in turns:
        starts.append(offset)
        offset += len(turn["text"]) + 1
    return "\n".join(t["text"] for t in turns), starts


def _segments(text: str, turn_starts: list[int], max_bytes: int) -> list[tuple[int, int]]:
    """(start, end) spans per turn; turns over the budget are split at spaces."""
    spans = []
    bounds = turn_starts + [len(text) + 1]
    for i, start in enumerate(turn_starts):
        end = bounds[i + 1] - 1
        while len(text[start:end].encode("utf-8")) > max_bytes:
            size, pos, cut = 0, start, start
            while pos < end:
                size += len(text[pos].encode("utf-8"))
                if size > max_bytes:
                    break
                if text[pos] == " ":
                    cut = pos
                pos += 1
            if cut == start:  # no space within budget: hard cut at the budget
                cut = pos
            spans.append((start, cut))
            start = cut + 1 if text[cut:cut + 1] == " " else cut
        spans.append((start, end))
    return spans


def chunk_document(text: str, turn_starts: list[int], max_bytes: int = MAX_CHUNK_BYTES) -> list[tuple[int, str]]:
    """Greedily pack consecutive turn spans into chunks of at most `max_bytes`."""
    chunks: list[tuple[int, str]] = []
    chunk_start = chunk_end = None
    for start, end in _segments(text, turn_starts, max_bytes):
        if chunk_start is not None and len(text[chunk_start:end].encode("utf-8")) <= max_bytes:
            chunk_end = end
            continue
        if chunk_start is not None:
            chunks.append((chunk_start, text[chunk_start:chunk_end]))
        chunk_start, chunk_end = start, end
    if chunk_start is not None and chunk_end > chunk_start:
        chunks.append((chunk_start, text[chunk_start:chunk_end]))
    return chunks


def _rebase(entity: dict, base: int, turn_starts: list[int]) -> dict:
    out = copy.deepcopy(entity)
    out["BeginOffset"] = entity["BeginOffset"] + base
    out["EndOffset"] = entity["EndOffset"] + base
    out["TurnIndex"] = bisect.bisect_right(turn_starts, out["BeginOffset"]) - 1
    for attribute in out.get("Attributes", []):
        if "BeginOffset" in attribute:
            attribute["BeginOffset"] += base
            attribute["EndOffset"] += base
    return out


def analyze(turns: list[dict], client) -> dict:
    """Call DetectEntitiesV2, InferICD10CM and InferRxNorm for every chunk."""
    text, turn_starts = build_document(turns)
    result = {"entities": [], "icd10cm": [], "rxnorm": [], "modelVersions": {}, "chunkCount": 0}
    calls = (
        ("entities", "DetectEntitiesV2", client.detect_entities_v2),
        ("icd10cm", "InferICD10CM", client.infer_icd10_cm),
        ("rxnorm", "InferRxNorm", client.infer_rx_norm),
    )
    for base, chunk in chunk_document(text, turn_starts, MAX_CHUNK_BYTES):
        if not chunk.strip():
            continue
        result["chunkCount"] += 1
        for field, api, call in calls:
            response = call(Text=chunk)
            result["modelVersions"][api] = response.get("ModelVersion")
            result[field].extend(_rebase(e, base, turn_starts) for e in response.get("Entities", []))
    return result
