# MyNotes

Architecture technique complète : [schéma Mermaid](docs/architecture.mmd) · [version SVG](docs/architecture.svg) · [guide](docs/architecture.md).

Single-user desktop study tool: courses, documents, notes, chat, and slides.

## Local speaker labels with Voxtral

**Settings → Microphone** includes a 0–120% input-level slider. It reads the actual
Linux/PipeWire default microphone level and applies your choice on release or with
the arrow keys, including during capture. This is a system-level setting shared with
other applications; no Cloud Temple connection is needed to adjust it.

In **Live notes**, click **Download speaker models (~120 MB)** once, then enable
**Distinguish speakers (local)** before recording. Keep the speaker count on Auto,
or choose the known count. Voxtral still supplies live text; after **Stop & summarize**,
local CPU processing produces a separate speaker-labelled transcript with estimated
timestamps. The summary uses that transcript together with your personal notes.

The local pipeline is free: Sherpa-ONNX with INT8 Pyannote segmentation and TitaNet
Small (~42 MB combined), plus multilingual faster-whisper tiny (~75 MB) for acoustic
word anchors. Matching preserves Voxtral's original words; unmatched or overlapping
speech is labelled **Speaker unknown**. This is approximate acoustic/text alignment,
not guaranteed forced alignment or recognition of real people's names. Cloud Temple
transcription and summaries retain their normal provider charges.

Models load in a disposable subprocess only after recording, with two inference
threads, and are released after processing. Closing the backend terminates that child.
The original raw transcript remains available separately. Temporary 16-kHz PCM16 WAV
audio uses about 115 MB/hour of disk; it is deleted after the derived transcript is
saved successfully, or retained on failure for **Retry speaker detection**. Models and
temporary audio are excluded from backups. A raw-summary fallback is available on
failure. Limits: four hours of audio and 30,000 transcript words per speaker job;
memory and processing time grow with session length.

Real-model smoke check (public audio, no paid API call):

```bash
uv run --locked python scripts/diarization-smoke.py --models /tmp/opencode/mynotes-speaker-models
```

Electron + React/TypeScript + Python/FastAPI + SQLite. One app, one backend, one database. No external search server, message broker, or vector database.

## Prerequisites

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| Python | 3.12+ | Managed exclusively via `uv` |
| Node.js | Current LTS | For the frontend build |
| Linux | Recommended | Linux-first; other OS packaging unverified |

Optional tools (not required for basic use):

- **Typst CLI** (Apache-2.0) — compiles `.typ` notes to PDF
- **Tesseract OCR** (Apache-2.0) — extracts text from scanned PDF pages
- **Marp Chromium** — Marp's PDF export uses Puppeteer's bundled Chromium; no separate install is required for HTML export

> Cloud Temple API endpoints, model names, and transcription response formats (including timestamp support) are **not yet confirmed**. This documentation does not assume specific capabilities.

## Quick start

```bash
# Install the backend dependencies
uv sync --locked

# Install the frontend and desktop dependencies
npm ci

# Build the UI and launch Electron (starts the backend automatically)
npm start
```

For the built Linux desktop app, run `./out/MyNotes-linux-x64/MyNotes`. The portable
ZIP is under `out/make/zip/linux/x64/`. Local notes, imports, search, and saving pasted transcripts require no Cloud Temple
credentials. Enter your LLMaaS key, endpoint details, and model names in **Settings**
before testing AI requests. Live provider calls have not been verified with real credentials.

### Cloud Temple model choices

In **Settings**, enter your API key and use **Save & connect** to retrieve
the exact IDs available to your account. Choose models independently:

- **Chat model:** grounded document chat and slide drafts.
- **General chat model:** ordinary chat without document access.
- **Audio summary model:** summaries of selected saved transcripts.
- **Document summary model:** summaries of selected documents.
- **Fast model:** metadata suggestions; uses the chat model if left blank.
- **Vision model:** an image-capable model for page and diagram descriptions.
- **Live transcription model:** the engine ID required by `/v1/realtime`.
  The public catalogue may list `voxtral`, while the realtime service expects
  `mistralai/Voxtral-Mini-4B-Realtime-2602`. The app maps the documented alias
  to this engine ID instead of assuming both endpoints accept the same name.

Model fields accept compatible IDs from any family, including Qwen, GPT-OSS, and
Mistral. The model list does not certify every model supports every feature.
The base URL is `https://api.ai.cloud-temple.com/v1`; the default request timeout is
60 seconds. Chat uses `/chat/completions`, and live transcription uses
WebSocket `/v1/realtime`. Keep API keys in Settings; unrelated OVH/S3, external MCP,
web-app session, and embedding settings are not needed by MyNotes.

Official references: [API documentation](https://docs.cloud-temple.com/llmaas/api)
and [model catalogue](https://docs.cloud-temple.com/llmaas/models).

Responsiveness-oriented defaults are `gpt-oss:120b` for general chat and transcript
summaries, `qwen3.6:27b` for selected-file questions and document summaries, and
`qwen3-vl:30b` for vision. Larger models remain selectable. These defaults are based
on Cloud Temple's published catalogue, not a latency benchmark with your credentials.

Live Notes streams microphone PCM in real-time to Cloud Temple Voxtral via the `/v1/realtime` endpoint and keeps the transcript text. **Pause / Resume**, **Stop recording** and **Summarize session** are separate controls; summaries use the configured `audio_summary_model`. Optional speaker detection records temporary local audio for post-Stop processing. Enter your API key in **Settings** → Save & connect first to enable streaming.

Live transcription stays active when switching pages. Personal notes remain editable
during the session and are included with the transcript in the AI notes. Choose
**Lecture** or **Meeting**, an output language, and optional names/vocabulary. The
language setting guides AI notes, not Voxtral's automatic speech recognition.
Summaries are streamed and concise; uncertain recognition is flagged rather than
asserted as verified facts. AI transcript review is optional and keeps the original.
Personal notes use Markdown source with Heading, Bold, List and Task shortcuts,
an editable/preview toggle, local draft recovery and `.md` export. The summary
receives the same notes alongside the transcript.

**Save as…** opens the native filename/folder picker; **Save to library** keeps a
managed note. **Delete from library** removes a note/transcript from browsing and
search; immutable versions already used as evidence remain available to old citations.

### Tool-using chat

The assistant's **Discussions** sidebar lets you create and reopen independent
conversations, each with its own saved history and context. Automatic summaries
are kept out of assistant conversations.

Choose **Search the web** in the task selector to retrieve public pages before
the model answers, or paste a public HTTPS URL into that mode. It works without
OpenCode or model-native tool calling. Consulted sources appear as clickable links
that open in your browser. Search-provider blocks are reported; DuckDuckGo has
returned captchas on this machine, with Bing RSS used as a fallback. Search
relevance varies, so a known public URL is the most direct verification path.

The **Agent harness** uses OpenCode for a multi-step tool loop, with visible tool
steps and conversation continuation. In Settings, add **Allowed directories** with
the native folder picker. The agent can list/search/read those folders and the
selected MyNotes sources. Real-path checks prevent symlink escapes; credential
files and arbitrary shell commands are inaccessible. Provider secrets remain in
the backend and are passed to the child process via environment interpolation,
never written into a project configuration. The app isolates the agent's configuration,
plugins and runtime state from your normal OpenCode workspace. OpenCode is bundled
when available to `npm run build:tools`, otherwise an installed CLI is required.

The top-bar night-mode button persists the theme. Liquid-glass panels use native
CSS, with readable colors and a reduced-motion fallback.

### Build and verify the Linux package

```bash
npm run package
MYNOTES_SMOKE_TEST=1 MYNOTES_TEST_DATA_DIR=/tmp/mynotes-smoke ./out/MyNotes-linux-x64/MyNotes
```

The package contains its Python runtime, standalone Node, Marp dependencies, and
headless browser. The desktop smoke checks notes, immutable sources, search,
recording IPC, actual HTML/PDF exports, and backup/restore. Run it with a separate
test data directory because it creates test documents. Typst compilation and local
OCR use optional system-installed Typst and Tesseract. See `docs/verification.md`
for the verification boundary.

## Development

| Command | Purpose |
|---------|---------|
| `uv sync --locked` | Install backend dependencies from `uv.lock` |
| `uv run --extra dev python -m pytest -q` | Run backend tests |
| `uv run --extra dev python -m ruff check src/mynotes tests` | Lint backend code |
| `npm run build:backend` | Produce a PyInstaller bundle (packaging unverified) |
| `npm ci` | Install frontend dependencies |
| `npm start` | Build the UI and launch the desktop app and backend |
| `npm run dev` | Start the UI-only Vite server; it does not provide the Electron bridge |
| `npm run build` | Build React app for production |
| `npm test` | Run frontend tests (Vitest) |

`uv.lock` pins Python dependencies. Never commit `__pycache__`, `.venv`, or generated output.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                     Electron shell                  │
│  ┌──────────────┐    ┌──────────────────────────┐   │
│  │  React/TS UI │    │  FastAPI backend (Python) │   │
│  │  (Vite / prod)│    │  127.0.0.1 + random port  │   │
│  └──────┬───────┘    └──────────┬───────────────┘   │
│         │                       │                    │
│         │    authenticated      │                    │
│         │  HTTP requests        │                    │
│         │                       │                    │
│         │              ┌────────┴────────┐          │
│         │              │  SQLite + FTS5   │          │
│         │              │  + background    │          │
│         │              │  worker          │          │
│         │              └─────────────────┘          │
│         │                       │                    │
│         │              ┌────────┴────────┐          │
│         │              │  Managed files   │          │
│         │              │  (originals,     │          │
│         │              │   audio, exports)│          │
│         │              └─────────────────┘          │
│                                                     │
│  Electron manages the backend lifecycle:            │
│  - Launches it on a random localhost port           │
│  - Waits for readiness signal                       │
│  - Terminates it on exit                            │
└─────────────────────────────────────────────────────┘
         │
         │  HTTP (authenticated)
         ▼
┌───────────────────────┐
│  Cloud Temple          │
│  (verify endpoints &   │
│   model licenses)      │
└───────────────────────┘
```

## Retrieval and search

### How answers are generated

**Default path — direct context.** When you select a document, course, or chapter, MyNotes sends its **full extracted text** to the AI model, provided it fits within the model's context window. Every passage carries an immutable source locator (document version ID + page/slide/block reference). This is the simplest, most reliable path.

**Lexical fallback — FTS5 search.** If the selected material exceeds the context budget, MyNotes falls back to SQLite FTS5 lexical search within the same scoped collection. It returns a bounded set of matching passages. The app signals that passage selection is being used — it never silently truncates a document and implies it was fully read.

### What FTS5 can and cannot do

FTS5 finds exact words, phrases, and prefix matches. It does **not** understand synonyms, paraphrases, or semantic relationships.

**Example limitation:** Searching for `"memorizing training examples"` may not find a passage written only as `"overfitting"`. The term is different, even though the concept is the same.

### Evaluation

Keep roughly ten real questions with known relevant passages as a small evaluation set. Measure **missed evidence** (relevant passages that were not found), not just how fluent the generated answer is.

### Future: semantic search (not implemented)

If lexical limits are demonstrated by evaluation, a semantic search experiment may be added. This would use an embedding model to produce numerical representations of passages and questions, compared via cosine similarity. A hybrid approach combining lexical and semantic rankings (reciprocal rank fusion) is also conceivable.

**No embedding dependencies exist in this version.** The feature is described here as a possible next step only — it requires no installation and is not part of the current scope.

## Notes

- Create notes in **Markdown** (rendered preview) or **Typst** (compiled PDF preview)
- Import PDF, PPTX, HTML, and Typst projects
- Audio recordings are captured via the browser's `getUserMedia` / `MediaRecorder` APIs and stored locally

## Files on laptop

Folders added in Settings appear as a nested, scrollable file tree. Select a
text file to edit its original path, or select a folder to create a new note.
**Save to original file** writes to disk; recovery drafts alone do not save the
original. Concurrent external edits are reported as conflicts.

The Presentations tab has been removed from desktop navigation. Existing slide
data and legacy MCP/export endpoints remain available for compatibility.

## OpenCode MCP configuration

MyNotes exposes its material-reading and slide-export capabilities to AI clients (e.g., OpenCode) via the Model Context Protocol over stdio.

### Setup

1. Open `docs/opencode-mcp.json`
2. Replace `/absolute/path/to/MyNotes` with your actual absolute project path
3. Validate the configuration against [OpenCode's published MCP schema](https://opencode.ai/docs/mcp-servers/)
4. Restart OpenCode

The configuration contains only the launch command — no API keys or tokens. Tokens are ephemeral, scoped to read material and manage decks only, and rotated on each app restart.

```json
{
  "mcp": {
    "mynotes": {
      "type": "local",
      "command": ["uv", "run", "--project", "/absolute/path/to/MyNotes", "--locked", "python", "-m", "mynotes", "mcp"],
      "enabled": true
    }
  }
}
```

### Available tools

| Tool | Purpose |
|------|---------|
| `get_material` | Read source text, citation IDs, and visual references |
| `get_deck` | Read an existing deck and its revision |
| `save_deck` | Create or replace a deck's Markdown with source references |
| `export_deck` | Queue HTML/PDF export, returns a job ID |
| `get_job` | Poll job status and retrieve artifact references |

For cross-machine use, install and configure MyNotes on each target machine. Remote exposure is outside this local tool interface.

## Jobs and long-running tasks

Imports, transcription, Typst compilation, and slide generation/export are asynchronous:

```
POST /imports → 202 Accepted { job_id, status_url }
GET  /jobs/{job_id} → state, progress, results or errors
```

The UI polls status while work continues. Short operations remain synchronous.

## Security

- **Encrypted credentials at rest.** API keys are encrypted with Fernet before storage.
- **Electron safeStorage.** The encryption key is protected by Electron's OS-backed secure storage.
- **Linux `basic_text` is not accepted.** Linux's insecure `basic_text` safeStorage backend is not treated as encrypted persistent storage. If no secure backend is available, credentials are stored **session-only** until a secure store is configured.
- **No secrets in Git.** `.gitignore` excludes credentials, keys, recordings, and generated output. All examples use placeholders.
- **No frontend access to secrets.** Renderer Node access is disabled; context isolation is enabled. API credentials are never bundled with the frontend.

## Backup and restore

Backup includes a consistent database snapshot plus all referenced original and derived files. **API credentials are excluded by default** and can be re-entered after restore.

## Building and packaging

### Backend

```bash
uv run python -m mynotes build
```

Produces a PyInstaller bundle. End users do not need `uv` or Python installed.

### Frontend

```bash
npm run build
```

Bundles the React app for production.

### Installer

The Electron app is packaged with Electron Forge into a platform-specific installer. **Installer acceptance testing has not yet been verified** on a clean target machine.

## Files and directories

| Path | Contents |
|------|----------|
| `src/mynotes/` | Python backend (FastAPI, worker, imports, MCP bridge) |
| `src/ui/` | React frontend components and API client |
| `tests/` | Backend and frontend tests |
| `docs/opencode-mcp.json` | OpenCode MCP configuration template |
| `docs/verification.md` | Plan check mapping: automated tests vs. external verification |
| `docs/licenses.md` | Declared dependency licenses (metadata only) |
| `.gitignore` | Excluded files (secrets, data, generated output) |
