"""Real CPU model/CLI smoke check; public JFK audio, no cloud key or paid calls.

uv run --locked python scripts/diarization-smoke.py --models /tmp/opencode/mynotes-speaker-models
Add --backend out/MyNotes-linux-x64/resources/backend/mynotes/mynotes to test the package.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from mynotes.diarization import model_status, setup_models

parser = argparse.ArgumentParser()
parser.add_argument("--models", type=Path, required=True)
parser.add_argument("--backend", type=Path)
args = parser.parse_args()
if not model_status(args.models)["ready"]:
    setup_models(args.models, on_progress=lambda stage, progress: print(stage, flush=True))

with tempfile.TemporaryDirectory(prefix="mynotes-speaker-check-", dir="/tmp/opencode") as folder:
    root = Path(folder)
    audio = root / "jfk.wav"
    response = httpx.get("https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/samples/jfk.wav",
                         follow_redirects=True, timeout=60)
    response.raise_for_status()
    assert 44 < len(response.content) < 2_000_000
    audio.write_bytes(response.content)
    original = "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country."
    output = root / "result.json"
    options = {"audio_path": str(audio), "transcript": original, "models_dir": str(args.models.resolve()),
               "output_path": str(output), "language": "en", "num_speakers": 1}
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, "-m", "mynotes"]
    subprocess.run(command + ["diarize"], input=json.dumps(options), text=True, check=True, timeout=180)
    result = json.loads(output.read_text())
    assert "".join(s["text"] for s in result["segments"]) == original
    assert any(s["speaker"] == "Speaker 1" and s["start"] is not None for s in result["segments"])
    assert result["unknown_words"] < result["total_words"] / 2
    print(json.dumps({"check": "actual CPU diarization + tiny anchors + raw-text alignment",
                      "elapsed_seconds": result["elapsed_seconds"], "unknown_words": result["unknown_words"],
                      "total_words": result["total_words"], "text": result["text"]}, indent=2))
