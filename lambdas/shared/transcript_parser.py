"""Turn Amazon Transcribe Medical output into dialogue turns.

Two modes:
- speaker labels (mono audio): the first speaker heard is labelled "Clinician", the second
  "Patient", others "Speaker N" (the sample consults always open with the clinician);
- channel identification (stereo audio): channel 0 is the clinician and channel 1 the patient,
  by the convention of the stereo sample (clinician left, patient right).
"""

ROLE_ORDER = ("Clinician", "Patient")
CHANNEL_ROLES = {"ch_0": "Clinician", "ch_1": "Patient"}


def _time_key(value) -> str | None:
    return None if value is None else f"{float(value):.3f}"


def _content(item: dict) -> tuple[str, float | None]:
    alt = (item.get("alternatives") or [{}])[0]
    try:
        confidence = float(alt.get("confidence", ""))
    except ValueError:
        confidence = None
    return alt.get("content", ""), confidence


def _words_from_speaker_labels(results: dict) -> list[dict]:
    speaker_at = {}
    for segment in results.get("speaker_labels", {}).get("segments", []):
        for item in segment.get("items", []):
            speaker_at[_time_key(item["start_time"])] = item["speaker_label"]
    words, last_speaker = [], "spk_0"
    for item in results.get("items", []):
        content, confidence = _content(item)
        if not content:
            continue
        if item.get("type") == "punctuation":
            if words:
                words[-1]["text"] += content
            continue
        speaker = speaker_at.get(_time_key(item.get("start_time")), last_speaker)
        last_speaker = speaker
        words.append({"speaker": speaker, "text": content, "start": float(item.get("start_time", 0)),
                      "end": float(item.get("end_time", 0)), "confidence": confidence})
    return words


def _words_from_channels(results: dict) -> list[dict]:
    words = []
    for channel in results["channel_labels"].get("channels", []):
        label, first = channel.get("channel_label"), len(words)
        for item in channel.get("items", []):
            content, confidence = _content(item)
            if not content:
                continue
            if item.get("type") == "punctuation":
                if len(words) > first:  # attach to this channel's previous word
                    words[-1]["text"] += content
                continue
            words.append({"speaker": label, "text": content, "start": float(item.get("start_time", 0)),
                          "end": float(item.get("end_time", 0)), "confidence": confidence})
    return sorted(words, key=lambda w: w["start"])


def role_method(transcribe_json: dict) -> str:
    return "channel" if transcribe_json.get("results", {}).get("channel_labels") else "speaker-order"


def parse_turns(transcribe_json: dict) -> list[dict]:
    results = transcribe_json.get("results", {})
    channels = bool(results.get("channel_labels"))
    words = _words_from_channels(results) if channels else _words_from_speaker_labels(results)

    turns: list[dict] = []
    roles: dict[str, str] = {}
    for word in words:
        if not turns or word["speaker"] != turns[-1]["speaker"]:
            if word["speaker"] not in roles:
                n = len(roles)
                roles[word["speaker"]] = (CHANNEL_ROLES.get(word["speaker"], f"Speaker {n + 1}") if channels
                                          else ROLE_ORDER[n] if n < len(ROLE_ORDER) else f"Speaker {n + 1}")
            turns.append({"index": len(turns), "speaker": word["speaker"], "role": roles[word["speaker"]],
                          "text": word["text"], "startTime": word["start"], "endTime": word["end"],
                          "_confidences": []})
        else:
            turns[-1]["text"] += " " + word["text"]
            turns[-1]["endTime"] = word["end"]
        if word["confidence"] is not None:
            turns[-1]["_confidences"].append(word["confidence"])

    for turn in turns:
        confidences = turn.pop("_confidences")
        turn["confidence"] = round(sum(confidences) / len(confidences), 3) if confidences else None
    return turns


def transcript_confidence(turns: list[dict]) -> float | None:
    values = [t["confidence"] for t in turns if t.get("confidence") is not None]
    return round(sum(values) / len(values), 3) if values else None


def labelled_transcript(turns: list[dict]) -> str:
    return "\n".join(f"{t['role']}: {t['text']}" for t in turns)
