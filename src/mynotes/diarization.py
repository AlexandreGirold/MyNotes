"""Optional CPU-only speaker labels. ML libraries load only in the analysis child."""
from __future__ import annotations

import json
import re
import shutil
import tarfile
import tempfile
import time
import unicodedata
import wave
from difflib import SequenceMatcher
from pathlib import Path

SEG_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMB_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx"
ALIGN_URL = "https://huggingface.co/Systran/faster-whisper-tiny/resolve/main/"
MODEL_FILES = ("segmentation.int8.onnx", "embedding.onnx", "alignment/model.bin",
               "alignment/config.json", "alignment/tokenizer.json", "alignment/vocabulary.txt")
MAX_SECONDS = 4 * 3600


def model_status(models_dir: Path) -> dict:
    missing = [p for p in MODEL_FILES if not (models_dir / p).is_file()
               or (models_dir / p).stat().st_size == 0]
    return {"ready": not missing, "missing": missing, "threads": 2,
            "model_download_mb": 120}


def _download(url: str, path: Path, limit: int = 100 * 1024 * 1024) -> None:
    import httpx
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            size = 0
            with temp.open("wb") as output:
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("Model download exceeded its size limit")
                    output.write(chunk)
        if not size:
            raise ValueError("Empty model download")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def setup_models(models_dir: Path, on_progress=None) -> dict:
    """Explicit download, atomic per file; readiness requires all six files."""
    models_dir.mkdir(parents=True, exist_ok=True)
    missing = set(model_status(models_dir)["missing"])
    if MODEL_FILES[0] in missing:
        if on_progress:
            on_progress("Downloading speaker segmentation", 0.05)
        with tempfile.TemporaryDirectory(dir=models_dir) as folder:
            archive = Path(folder) / "segmentation.tar.bz2"
            _download(SEG_URL, archive)
            with tarfile.open(archive, "r:bz2") as tar:
                member = next(m for m in tar.getmembers()
                              if m.isfile() and Path(m.name).name == "model.int8.onnx")
                if not 0 < member.size < 10 * 1024 * 1024:
                    raise ValueError("Invalid segmentation model size")
                with tar.extractfile(member) as source, (Path(folder) / "model").open("wb") as target:
                    shutil.copyfileobj(source, target)
                (Path(folder) / "model").replace(models_dir / MODEL_FILES[0])
    if MODEL_FILES[1] in missing:
        if on_progress:
            on_progress("Downloading voice model", 0.2)
        _download(EMB_URL, models_dir / MODEL_FILES[1])
    for i, name in enumerate(MODEL_FILES[2:]):
        if on_progress:
            on_progress("Downloading tiny local text alignment", 0.4 + i * 0.14)
        if name in missing:
            _download(ALIGN_URL + Path(name).name, models_dir / name)
    result = model_status(models_dir)
    if not result["ready"]:
        raise ValueError("Model setup incomplete")
    return result


def _normalise(word: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", word).casefold() if c.isalnum())


def label_transcript(transcript: str, words: list[dict], turns: list[dict]) -> dict:
    """Match acoustic anchors to raw words, never fabricate identity for gaps."""
    tokens = list(re.finditer(r"\S+", transcript))
    aligned = [None] * len(tokens)
    # ponytail: sequence alignment can be quadratic; cap transcript tokens before ML.
    matcher = SequenceMatcher(None, [_normalise(t.group()) for t in tokens],
                              [_normalise(w["word"]) for w in words], autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for offset in range(size):
            if _normalise(tokens[a + offset].group()):
                aligned[a + offset] = words[b + offset]
    segments = []
    unknown = 0
    for i, (token, word) in enumerate(zip(tokens, aligned)):
        speaker, start, end = "Speaker unknown", None, None
        if word:
            start, end = float(word["start"]), float(word["end"])
            duration = end - start
            overlaps = {}
            for turn in turns:
                overlap = max(0, min(end, turn["end"]) - max(start, turn["start"]))
                overlaps[turn["speaker"]] = max(overlaps.get(turn["speaker"], 0), overlap)
            ranked = sorted(overlaps.items(), key=lambda p: p[1], reverse=True)
            # A word crossing speakers or overlapping voices remains uncertain.
            if (duration > 0 and ranked and ranked[0][1] >= duration * 0.5
                    and (len(ranked) == 1 or ranked[1][1] < duration * 0.2)):
                speaker = f"Speaker {int(ranked[0][0]) + 1}"
        if speaker == "Speaker unknown":
            unknown += 1
        left = 0 if i == 0 else token.start()
        right = tokens[i + 1].start() if i + 1 < len(tokens) else len(transcript)
        part = transcript[left:right]
        previous = segments[-1] if segments else None
        if previous and previous["speaker"] == speaker and (
            start is None or previous["end"] is None or start - previous["end"] <= 2
        ):
            previous["text"] += part
            if end is not None:
                previous["end"] = end
        else:
            segments.append({"speaker": speaker, "start": start, "end": end, "text": part})
    def stamp(value):
        return f"{int(value) // 3600:02}:{int(value) // 60 % 60:02}:{value % 60:05.2f}"
    lines = []
    for segment in segments:
        timing = ""
        if segment["start"] is not None:
            timing = f"[{stamp(segment['start'])}–{stamp(segment['end'])}] "
        lines.append(f"{timing}{segment['speaker']}: {segment['text'].strip()}")
    warnings = ["Speaker labels and local alignment are estimates; review disagreements and overlapping speech."]
    if unknown:
        warnings.append(f"{unknown}/{len(tokens)} words have uncertain attribution (Speaker unknown).")
    return {"text": "\n\n".join(lines), "segments": segments, "warnings": warnings,
            "unknown_words": unknown, "total_words": len(tokens)}


def analyze(audio_path: Path, transcript: str, models_dir: Path,
            language: str = "auto", num_speakers: int = 0) -> dict:
    """Called in a disposable child, with no automatic model/network downloads."""
    if language not in {"auto", "fr", "en"} or not 0 <= num_speakers <= 10:
        raise ValueError("Invalid diarization options")
    if len(transcript.split()) > 30000:
        raise ValueError("Diarization supports up to 30,000 transcript words")
    if not model_status(models_dir)["ready"]:
        raise ValueError("Download speaker models before recording")
    started = time.monotonic()
    import numpy as np
    import sherpa_onnx
    with wave.open(str(audio_path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("Diarization requires mono PCM16 16 kHz WAV")
        if audio.getnframes() > MAX_SECONDS * 16000:
            raise ValueError("Diarization supports sessions up to four hours")
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32)
        samples /= 32768
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(models_dir / MODEL_FILES[0])), num_threads=2, provider="cpu"),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(models_dir / MODEL_FILES[1]), num_threads=2, provider="cpu"),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers or -1, threshold=0.5),
        min_duration_on=0.3, min_duration_off=0.5)
    if not config.validate():
        raise ValueError("Invalid speaker model configuration")
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    turns = [{"start": s.start, "end": s.end, "speaker": s.speaker}
             for s in diarizer.process(samples).sort_by_start_time()]
    del diarizer
    from faster_whisper import WhisperModel
    model = WhisperModel(str(models_dir / "alignment"), device="cpu", compute_type="int8",
                         cpu_threads=2, num_workers=1, local_files_only=True)
    segments, _ = model.transcribe(samples, language=None if language == "auto" else language,
                                  beam_size=1, word_timestamps=True, condition_on_previous_text=False)
    words = [{"word": w.word, "start": w.start, "end": w.end}
             for segment in segments for w in (segment.words or [])]
    result = label_transcript(transcript, words, turns)
    return {**result, "turns": turns, "elapsed_seconds": round(time.monotonic() - started, 2)}


def child_main() -> None:
    import sys
    payload = json.load(sys.stdin)
    result = analyze(Path(payload["audio_path"]), payload["transcript"],
                     Path(payload["models_dir"]), payload.get("language", "auto"),
                     payload.get("num_speakers", 0))
    Path(payload["output_path"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
