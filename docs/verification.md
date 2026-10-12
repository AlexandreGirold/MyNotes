# Verification — Linux desktop preview

## Workspace usability and explicit web research (2026-10-12)

- 532 backend tests (one existing export-environment skip), 30 frontend tests; Ruff and TypeScript/Vite pass.
- Fixed the file-tree root cause: legacy `.view-notes li` flex styling affected every nested folder. Styling is now scoped to the library list; the file browser has its own nested row layout and bounded scrolling. Desktop acceptance checks child rows appear below their parent and reviews screenshots with 32 fixture directories.
- The assistant has a visible Discussions sidebar, independent persistent conversations and a native **Search the web** task. This task fetches evidence before the provider call, without depending on OpenCode availability or model-native tool calling. It reads up to three public pages; source links open externally via HTTPS-only Electron handling.
- Packaged acceptance selected Search the web, fetched the real public Cloud Temple catalogue, sent that fetched text to a local model simulator and rendered a clickable source. This verifies the Internet/UI/provider wiring, not production model answer quality or search relevance. Real DuckDuckGo search was blocked by a captcha; Bing RSS returned results of poor relevance for the Cloud Temple query. A known public HTTPS URL worked.
- Personal notes have Markdown formatting shortcuts and a preview; source text, local recovery and `.md` export are checked. The packaged desktop check rendered bold notes, returned to editing and included them in the summary.
- Presentations is absent from navigation; existing slide records are not deleted.
- A malformed inherited `npm_config_user_agent` reported `npm/undefined` to Forge. Packaging succeeded with that variable removed for the invocation (`env -u npm_config_user_agent npm run electron:make`); no dependency or user configuration was changed.

## Assistant conversations and public web (2026-10-09)

- 530 backend tests, one existing skip; 28 frontend tests. Ruff and TypeScript/Vite pass.
- Conversations persist separately in SQLite, retain their own agent session and direct-chat context, and can be selected or created in the assistant. The old history is migrated without deleting records; completed summary jobs identify old summary entries so they are excluded from assistant history.
- Automatic audio/document summaries and transcript cleanup remain saved but are excluded from assistant conversations. Direct chat includes the newest 20 turns within a 24000-character context budget; full saved history remains accessible in its conversation.
- The eight-step OpenCode agent limit is removed. The real CLI/MCP simulator successfully completes ten tool calls and an answer. Execution still has the existing 240-second process deadline; removing a step limit does not remove network/process timeouts.
- Public search/read tools are exposed over MCP when Agent harness is enabled. HTTPS connections pin validated public DNS addresses and verify TLS against the hostname; redirects, query preservation, private destinations, body limits and MCP registration are checked.
- A real public Cloud Temple page was fetched successfully (16000-character bounded text). DuckDuckGo returned an anti-bot challenge, so public Bing RSS is used as a fallback. Search relevance is provider-dependent; results are candidate sources, not verified facts. Direct chat and the standalone summary button do not invoke web tools.
- The rebuilt Linux package passed desktop acceptance for separate persisted conversations, the web-tool indicator, summaries excluded from assistant history, microphone controls and Pause/Resume/Stop/separate summary. Its frozen MCP backend also fetched the real Cloud Temple page over HTTPS successfully; no production model call was used for this check.

## Controls and model catalogue update (2026-10-09)

- Backend: 523 passed, one existing skip; frontend: 27 passed. TypeScript/Vite, Ruff and native microphone/PCM checks pass.
- Packaged Electron acceptance passed Pause → Resume → Stop without automatic summary → separate Summarize, background capture, personal notes and native export, against a local provider simulator.
- Pause is always visible, disabled before capture; Resume replaces it while paused. The packaged screenshot confirms the initial Pause and Summarize controls are visible.
- Microphone read/write uses the actual PipeWire source, validates delayed readback, disables browser automatic gain control in both capture flows, and provides explicit Apply / Read actual level controls. A failed apply rereads the actual value instead of displaying the requested value as saved. Acceptance preserves the current system level.
- Settings exposes all 32 models on Cloud Temple's published product page, native per-task selectors, custom IDs, account catalogue refresh, published throughput/context and descriptive strengths. Speed ranks are not a quality leaderboard; account access and advertised performance are distinct from verified local measurements.
- Canonical workspace file/conflict/security checks and nonblocking live checkpoints pass. Long-session remote EngineCore failures, including the reported crash after 22 minutes, are not resolved or explained by these short simulator checks.

## Local speaker detection update

- Backend regression: 461 passed, one existing export-environment skip, zero failures.
- Frontend regression: 22 passed, zero failures; TypeScript/Vite build and Ruff pass.
- New checks cover optional PCM capture, WAV finalization before job publication, model-readiness gating, raw-version preservation, unknown/overlapping attribution, derived transcript persistence, failure/retry audio retention, and summary source selection.
- Real local models were downloaded through the application's setup routine. An 11-second public JFK excerpt completed actual CPU diarization plus tiny acoustic anchors in 1.12 seconds; `/usr/bin/time` recorded 301052 KiB peak RSS for the check. This is a short single-speaker smoke measurement, not a long-session, French, or multi-speaker accuracy benchmark.
- Source: `scripts/diarization-smoke.py`. Audio comes from whisper.cpp's public example; no production API calls or user audio are used.
- Electron source-mode smoke with real CPU models passed background recording, personal notes, derived transcript selection, summary, night mode and native text writing. The fake microphone differs from simulator text, so uncertain speaker labels are expected; it does not measure diarization accuracy.
- The packaged Linux executable passed that same speaker-aware desktop smoke. The frozen backend also processed the public JFK sample with the actual bundled inference libraries, preserving the raw words and estimated timestamps.
- Isolated desktop simulator tests use memory-only placeholder credentials and their own runtime discovery file. They do not access the OS keyring or overwrite discovery for an already-open real app; encrypted credential persistence is a separate verification path.
- When the physical desktop was locked/switched to the login screen, GUI tests stalled after backend startup. Source-mode and final packaged acceptance then passed on an isolated Xvfb display with software/offscreen rendering. Xvfb is a verification-only tool extracted under `/tmp/opencode`, not an application dependency or a change to the user's desktop session.
- The whole-project Mermaid source is `docs/architecture.mmd`; it has been parsed/rendered by Mermaid CLI to SVG and PNG. No diagram was published into an external account.

## Runnable checks

The microphone slider reads the actual Linux/PipeWire default-source volume and
accepts integer percentages from 0 to 120 through sender-validated Electron IPC.
Frontend checks cover release/keyboard application, 120% selection and unavailable
system control. The packaged microphone smoke checks actual read/write at the existing
level (without changing the user's choice), rejection of out-of-range input, slider
display and the normal live-capture/summary/export workflow.

```bash
uv run --locked --extra dev python -m pytest -q
uv run --locked --extra dev python -m ruff check src/mynotes
npm run build
npm test
node tests/packaging.test.cjs
npm run package
MYNOTES_SMOKE_TEST=1 MYNOTES_TEST_DATA_DIR=/tmp/mynotes-smoke ./out/MyNotes-linux-x64/MyNotes
```

Use a test data directory for the desktop smoke: it creates documents and performs
backup/restore. On a Linux desktop with the usual Electron system libraries, this
also checks that installed development commands are not used:

```bash
PATH=/nonexistent MYNOTES_SMOKE_TEST=1 MYNOTES_TEST_DATA_DIR=/tmp/mynotes-no-dev-runtime ./out/MyNotes-linux-x64/MyNotes
```

## What the checks cover

- Real SQLite/FTS5 retrieval, course scope, conservative context budgeting,
  full selected material before lexical fallback, versioned documents and jobs.
- Generated valid PDF fixtures, HTML text extraction, PPTX pictures and grouped
  shapes, managed files surviving job-directory cleanup.
- Real notes → updates → old source preservation → search → backup/restore,
  including restore-job status surviving database restoration.
- MCP bridge requests against the real tool-scoped API, saved-deck revision
  conflicts, missing-app errors, and restrictions on provider/settings access.
- Provider HTTP contracts and model routing using mock HTTP transports; no paid
  model calls are made by automated tests.
- Streamed lecture/French summary instructions, personal notes in context, output
  limit warnings, soft deletion while preserving old evidence, and native Save as
  requests/cancellation/saved-location feedback.
- A real OpenCode CLI + MCP loop against a local HTTP model simulator reads an
  allowed file and returns evidence from it; directory/credential scope is tested.
- Personal-note drafts and night mode survive remounts. The desktop acceptance test
  leaves the live page while audio continues, returns, summarizes both sources,
  checks night mode, and exports actual UTF-8 text through the native IPC writer.
  Its OS file dialog is replaced by a test picker response; manual picker UI remains
  a target-desktop check.
- API key kept out of SQLite settings, encrypted credential round trips, and
  invalid timestamp rejection.
- React note actions, API authentication and multipart headers, and delayed final
  recording chunks saved before recording finalization/upload.
- Voxtral relay against a real local WebSocket simulator: partial text arrives
  before stop, authentication is scoped, PCM frames are forwarded, and disconnects
  preserve received transcript text without writing audio files.
- `node --test tests/pcm-worklet.test.cjs` checks real PCM little-endian encoding
  and flushing. `uv run --locked python scripts/live-ui-smoke.py` uses Electron's
  fake microphone with a local provider to test the actual AudioWorklet, transcript
  appearing during capture, and the summary model request. Set
  `MYNOTES_PACKAGED_TEST=1` to run the same check against the built package.
- Real Marp/Node/headless-browser exports, selectable PDF text, source citations
  in PDF and manifest, and durable downloadable artifacts.
- Packaged Electron startup, preload bridge, authenticated backend, note updates,
  FTS search, recording IPC, real exports, backup/restore, and clean shutdown.

One legacy export test may be skipped because it expects a Python package named
`node`. Real bundled-export coverage is in `tests/test_acceptance.py` and the
packaged desktop smoke; the skip does not stand in for runtime export verification.

## Checks requiring user resources

- Live Cloud Temple chat, vision, transcription, actual account model access,
  costs, latency, and service-provided timestamps: require a real key entered in
  Settings. Published catalogue throughput is not a measured end-to-end latency.
- Real microphone capture and device permission behavior: require manual testing
  on the target desktop. Automated recording checks cover event ordering and IPC.
- Representative course questions and long-document answer quality: require a
  real course dataset and expected passages.
- Typst compilation and Tesseract OCR on representative projects/scans: require
  their optional system tools. Missing tools produce visible errors/warnings and
  preserve originals. Neither tool is bundled in this preview.
- Installer behavior on an independently provisioned clean machine, Windows,
  and macOS: not verified by the Linux host smoke.
- Live Voxtral endpoint/model availability, recognition quality and latency still
  require a real API key. Simulator results do not verify the live Cloud Temple
  service. The default UI streams audio transiently and saves transcript text only.

The Linux ZIP is a local preview artifact, not a published or signed release.
