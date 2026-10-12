from mynotes.diarization import MODEL_FILES, label_transcript, model_status


def test_labels_preserve_raw_words_and_recover_after_missing_word():
    raw = "Bonjour,  licorne\nmerci. Non !"
    words = [{"word": "Bonjour", "start": 0, "end": 1},
             {"word": "merci", "start": 1, "end": 2},
             {"word": "Non", "start": 3, "end": 4}]
    turns = [{"speaker": 0, "start": 0, "end": 2},
             {"speaker": 1, "start": 3, "end": 4}]
    result = label_transcript(raw, words, turns)
    assert "".join(s["text"] for s in result["segments"]) == raw
    assert [s["speaker"] for s in result["segments"]] == [
        "Speaker 1", "Speaker unknown", "Speaker 1", "Speaker 2", "Speaker unknown"]
    assert "[00:00:03.00–00:00:04.00] Speaker 2: Non" in result["text"]
    assert result["unknown_words"] == 2


def test_overlap_is_unknown_and_no_anchors_do_not_invent_speakers():
    word = [{"word": "oui", "start": 0, "end": 1}]
    turns = [{"speaker": n, "start": 0, "end": 1} for n in (0, 1)]
    assert label_transcript("oui", word, turns)["segments"][0]["speaker"] == "Speaker unknown"
    assert label_transcript("oui", [], turns)["unknown_words"] == 1


def test_readiness_requires_complete_nonempty_models(tmp_path):
    for name in MODEL_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")
    assert model_status(tmp_path)["ready"]
    (tmp_path / MODEL_FILES[-1]).write_bytes(b"")
    assert not model_status(tmp_path)["ready"]
