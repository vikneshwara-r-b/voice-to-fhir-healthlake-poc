from conftest import cm_entity
from shared.nlp import analyze, build_document, chunk_document


def _turns(*texts):
    return [{"index": i, "text": t} for i, t in enumerate(texts)]


def test_small_document_is_one_chunk():
    text, starts = build_document(_turns("Hello.", "Hi there."))
    assert text == "Hello.\nHi there."
    assert starts == [0, 7]
    assert chunk_document(text, starts) == [(0, text)]


def test_chunks_respect_byte_budget_and_turn_boundaries():
    turns = _turns("a" * 40, "b" * 40, "c" * 40)
    text, starts = build_document(turns)
    chunks = chunk_document(text, starts, max_bytes=90)
    assert chunks == [(0, "a" * 40 + "\n" + "b" * 40), (82, "c" * 40)]
    for base, chunk in chunks:
        assert text[base:base + len(chunk)] == chunk


def test_oversized_turn_is_split_at_spaces_with_multibyte_text():
    long_turn = " ".join(["ménière"] * 50)  # é is 2 bytes in UTF-8
    text, starts = build_document(_turns(long_turn))
    chunks = chunk_document(text, starts, max_bytes=100)
    assert len(chunks) > 1
    for base, chunk in chunks:
        assert len(chunk.encode("utf-8")) <= 100
        assert text[base:base + len(chunk)] == chunk
        assert not chunk.startswith(" ") and not chunk.endswith(" ")


class FakeComprehendMedical:
    """Finds fixed phrases in each chunk and reports chunk-relative offsets."""

    def __init__(self):
        self.calls = []

    def _find(self, text, phrase, category, codes):
        pos = text.find(phrase)
        return [] if pos < 0 else [cm_entity(phrase, category, begin=pos, traits=["DIAGNOSIS"], codes=codes)]

    def detect_entities_v2(self, Text):
        self.calls.append(("DetectEntitiesV2", len(Text)))
        return {"Entities": self._find(Text, "hypertension", "MEDICAL_CONDITION", []), "ModelVersion": "d1"}

    def infer_icd10_cm(self, Text):
        self.calls.append(("InferICD10CM", len(Text)))
        return {"Entities": self._find(Text, "hypertension", "MEDICAL_CONDITION",
                                       [("I10", "Essential (primary) hypertension", 0.9)]), "ModelVersion": "i1"}

    def infer_rx_norm(self, Text):
        self.calls.append(("InferRxNorm", len(Text)))
        return {"Entities": [], "ModelVersion": "r1"}


def test_analyze_rebases_offsets_onto_full_document(monkeypatch):
    monkeypatch.setattr("shared.nlp.MAX_CHUNK_BYTES", 40)
    turns = _turns("Good morning to you all.", "I have hypertension.")
    text, _ = build_document(turns)
    client = FakeComprehendMedical()

    result = analyze(turns, client)

    assert result["chunkCount"] == 2
    assert len(client.calls) == 6
    entity = result["icd10cm"][0]
    assert text[entity["BeginOffset"]:entity["EndOffset"]] == "hypertension"
    assert entity["TurnIndex"] == 1
    assert result["modelVersions"] == {"DetectEntitiesV2": "d1", "InferICD10CM": "i1", "InferRxNorm": "r1"}
