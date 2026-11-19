"""Repeatable local I/O timings; only disposable fixtures, no provider calls or user data.

Run: uv run --locked python scripts/performance-smoke.py --runs 5
"""
import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from mynotes.db import Database
from mynotes.workspace import atomic_write, list_directory, read_text


def measure(action, runs):
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        action()
        samples.append((time.perf_counter() - start) * 1000)
    return {"median_ms": round(statistics.median(samples), 3), "min_ms": round(min(samples), 3), "max_ms": round(max(samples), 3)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    report = {"runs": args.runs, "fixtures": "disposable local files; warm filesystem cache", "operations": {}}
    with tempfile.TemporaryDirectory(prefix="mynotes-perf-") as temporary:
        root = Path(temporary)
        files = root / "files"
        files.mkdir()
        for total in (1000, 5000):
            for i in range(0 if total == 1000 else 1000, total):
                (files / f"note-{i:05}.md").touch()
            def action():
                return list_directory(str(files), [str(files)])
            result = action()
            assert len(result["entries"]) == 200 and result["next_offset"] == 200
            timing = measure(action, args.runs)
            original_stat = Path.stat
            count = 0
            def counted_stat(path, *a, **kw):
                nonlocal count
                count += 1
                return original_stat(path, *a, **kw)
            with patch.object(Path, "stat", counted_stat):
                action()
            report["operations"][f"list_{total}_page_200"] = {**timing, "path_stat_calls": count}
        file = files / "large.md"
        text = "# Note\n" + "A useful observation.\n" * 48000
        saved = atomic_write(str(file), text, [str(files)])
        assert read_text(str(file), [str(files)])["text"] == text
        report["operations"]["read_1mb"] = measure(lambda: read_text(str(file), [str(files)]), args.runs)
        def write():
            nonlocal saved
            saved = atomic_write(str(file), text, [str(files)], saved["sha256"])
        report["operations"]["atomic_save_1mb"] = measure(write, args.runs)
        db = Database(root / "library.sqlite3")
        for i in range(200):
            db.save_document(f"Fixture {i}", "markdown", f"fixture-{i}.md", [{"text": f"PERFORMANCE_TOPIC_{i} private fixture only", "locator": {}, "origin": "fixture"}])
        assert db.search("PERFORMANCE_TOPIC_199")
        report["operations"]["fts_search_200_documents"] = measure(lambda: db.search("PERFORMANCE_TOPIC_199"), args.runs)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
