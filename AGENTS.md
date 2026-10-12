# MyNotes — Contributor & Agent Rules

USE up too 100 qwen subagents. The main model is the orchestrator and verifier (can als delegate to luna6 model on midium) the rest is done by qwenwhe

## Contributor workflow

- **uv only.** All Python dependency management and execution must use `uv`. No `pip`, no `poetry`, no `conda`.
  - `uv add <pkg>` to add dependencies.
  - `uv sync --locked` to install from `uv.lock`.
  - `uv run python -m mynotes` to execute backend code.
  - `uv.lock` is committed; never commit `__pycache__` or `.venv`.
- **No pip under any circumstances.** If an installation requires pip, flag it instead.
- **Secrets and data are never committed.** `.gitignore` excludes local credentials, application data, recordings, and generated output. Examples contain placeholders only.
- **Merge via clean diffs.** Commit messages describe what changed and why. Rebase before merge when requested.

## Architecture constraints

- **One app, one backend, one DB.** No external search server, message broker, or vector database.
- **Electron + React/TS + FastAPI + SQLite.** Do not add frameworks outside this stack without explicit approval.
- **No embeddings.** Semantic search is documented in the README as a future experiment only.
  Exception: local speaker embeddings are allowed internally for audio diarization; never for document search or a vector database.
- **SQLite FTS5 is the sole search/indexing mechanism.**
- **One background worker.** RabbitMQ/Kafka are not required for single-user throughput.
- **202 Accepted + polling** for all long-running operations.
- **Open-source tools only** for application infrastructure. Cloud Temple is an allowed commercial service but must be verified per-endpoint.
- **Vision, transcription timestamps, and Cloud Temple endpoints are unverified** until confirmed.

## Verify commands

The following commands are part of the development workflow. Not all are configured yet; mark them as "not configured" where applicable and fill them in when set up.

| Command | Purpose | Status |
|---------|---------|--------|
| `uv sync --locked` | Install dependencies from committed lock file | Not yet configured |
| `uv run python -m mynotes test` | Run backend tests | Not yet configured |
| `npm test` | Run frontend tests | Not yet configured |
| `npm run build` | Build React app for production | Not yet configured |
| `uv run python -m mynotes lint` | Lint backend code | Not yet configured |

## Secret & data exclusion

- **Never commit:** decryption keys, API credentials, generated/untracked files, `.env`, application data (`~/.local/share/mynotes` or equivalent), recordings, or compiled output.
- **Use placeholders** in any example or config committed to Git.
- **Verify with `git status`** before every commit. If any secret-like file appears staged, unstage it immediately.

## Agent delegation

When implementing from PLAN.md:

1. **Main agent owns all decisions, integration, and verification.** The main agent is responsible for the coherent whole.
2. **Up to 100 Qwen subagents may be launched** to implement independent parts of the plan in parallel. Each subagent should target a clearly bounded scope (e.g., "Phase 1: SQLite schema + FTS5", "Phase 2: FastAPI imports endpoint").
3. **Parallelize independent work.** Phase 1 sub-tasks, Phase 2 sub-tasks, and Phase 3 sub-tasks can run concurrently if their scopes do not overlap.
4. **Avoid redundant agent sprawl.** Each subagent should own a single coherent piece. Do not spawn multiple agents for the same file or scope.
5. **Luna reviewers may be used only if available.** If no Luna agents are available, the main agent performs all reviews.
6. **Qwen agents implement; main agent integrates.** Subagents produce working code for their scope. The main agent integrates, verifies, and resolves cross-cutting concerns (config, imports, shared types, secrets handling).
