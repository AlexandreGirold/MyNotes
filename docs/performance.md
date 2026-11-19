# Local performance checks — 2026-10-12

## Repeatable measurements

```bash
uv run --locked python scripts/performance-smoke.py --runs 5
```

The script uses disposable local files and a temporary SQLite database. It checks
real pagination, text round trips and FTS results before timing them. These are
warm-cache timings on this machine, not remote-model throughput or guarantees for
network-mounted folders.

| Operation | Before, median | After, median |
|---|---:|---:|
| List 200 entries from 1,000 files | 73.176 ms | 28.697 ms |
| List 200 entries from 5,000 files | 191.674 ms | 112.783 ms |
| `Path.stat` calls for 5,000 files | 25,000 | 5,000 |
| Read approximately 1 MiB | 0.899 ms | 0.930 ms |
| Atomically save approximately 1 MiB | 5.018 ms | 5.561 ms |
| FTS query across 200 documents | 0.787 ms | 0.805 ms |

Directory listing is about 41% faster at 5,000 files and uses 80% fewer stat calls.
It still scans/sorts the directory to preserve safe, stable alphabetical paging.
Roots are resolved once per listing; each candidate is still independently
resolved and checked for containment and credentials. No global authorization
cache is introduced.

Read/write/FTS differences are small local timing variation, not claimed gains.
Saving a note no longer refreshes the entire folder listing. Throttled recovery
storage and bounded Markdown previews avoid repeated full serialization and
thousands of preview DOM nodes.

## UI and failure checks

```bash
pnpm test
uv run --locked python -m pytest -q tests/test_workspace_regressions.py
MYNOTES_PACKAGED_TEST=1 MYNOTES_MIC_VOLUME_SMOKE_TEST=1 MYNOTES_WEB_SMOKE_TEST=1 \
  uv run --locked python scripts/live-ui-smoke.py
```

The desktop test opens native-picker responses in isolated fixture directories,
creates a subfolder and a real Markdown file, types 20 changes into a roughly
150 KiB note, checks actual autosave output and verifies zero directory refreshes
caused by saving. Source-mode software/offscreen rendering measured 35.7 ms
median input-to-next-frame, 64.8 ms maximum; autosave completed 1030 ms after the
last change, including its intentional one-second debounce. Final packaged
measurements are reported by the same script.

The test then changes the original externally, verifies the conflicting draft is
retained and autosave stops, reviews disk contents and explicitly saves the draft
against that reviewed hash. Unit checks also cover typing during an in-flight
save, duplicate pagination, bounded previews with the full note preserved,
credential/symlink boundaries and closing temporary file descriptors on failure.

The picker response is simulated; manual OS-dialog usability is not measured.
Provider replies and microphone audio are simulated, while local filesystem
writes, SQLite operations and the optional public-page read are real. No user
files, saved credentials or production model calls are used.
