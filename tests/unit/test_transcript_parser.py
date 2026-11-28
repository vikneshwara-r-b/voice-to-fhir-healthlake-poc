from conftest import transcribe_output
from shared.transcript_parser import labelled_transcript, parse_turns, transcript_confidence


def test_groups_words_into_turns_with_roles_and_punctuation():
    doc = transcribe_output([
        ("spk_0", "Good morning. What brings you in?"),
        ("spk_1", "My knee hurts."),
        ("spk_0", "Any fever?"),
    ])
    turns = parse_turns(doc)
    assert [(t["role"], t["text"]) for t in turns] == [
        ("Clinician", "Good morning. What brings you in?"),
        ("Patient", "My knee hurts."),
        ("Clinician", "Any fever?"),
    ]
    assert [t["index"] for t in turns] == [0, 1, 2]
    assert turns[0]["startTime"] == 0.0
    assert turns[1]["startTime"] > turns[0]["endTime"]


def test_first_speaker_is_clinician_even_if_labelled_spk_1():
    turns = parse_turns(transcribe_output([("spk_1", "Hello there."), ("spk_0", "Hi.")]))
    assert [t["role"] for t in turns] == ["Clinician", "Patient"]


def test_consecutive_segments_from_same_speaker_merge():
    turns = parse_turns(transcribe_output([("spk_0", "One."), ("spk_0", "Two."), ("spk_1", "Three.")]))
    assert [t["text"] for t in turns] == ["One. Two.", "Three."]


def test_without_speaker_labels_everything_is_one_turn():
    doc = transcribe_output([("spk_0", "Hello."), ("spk_1", "Hi.")])
    del doc["results"]["speaker_labels"]
    turns = parse_turns(doc)
    assert len(turns) == 1 and turns[0]["text"] == "Hello. Hi."


def test_empty_output_gives_no_turns():
    assert parse_turns({"results": {"items": []}}) == []


def test_confidence_and_labelled_text():
    turns = parse_turns(transcribe_output([("spk_0", "Hi."), ("spk_1", "Hello.")], confidence="0.9"))
    assert transcript_confidence(turns) == 0.9
    assert labelled_transcript(turns) == "Clinician: Hi.\nPatient: Hello."
