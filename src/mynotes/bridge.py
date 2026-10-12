"""MyNotes — thin local MCP bridge over stdio.

No database access, no background worker.  Every tool call reads the
ephemeral runtime discovery file, then forwards an authenticated HTTPX
request to the same FastAPI endpoints that the Electron UI uses.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime discovery
# ---------------------------------------------------------------------------

_RUNTIME_FILE_ENV = "MYNOTES_RUNTIME_FILE"
_DEFAULT_RUNTIME_PATH = (
    pathlib.Path.home() / ".local" / "state" / "mynotes" / "runtime.json"
)


def _discover() -> dict[str, Any]:
    """Read and validate the runtime discovery file.

    Returns the parsed JSON dict (must contain ``url`` and ``token``).
    Raises ``RuntimeError`` when the app is unavailable or the file is
    unreadable / invalid.
    """
    runtime_path = pathlib.Path(
        os.environ.get(_RUNTIME_FILE_ENV, str(_DEFAULT_RUNTIME_PATH))
    )

    if not runtime_path.exists():
        raise RuntimeError(
            "MyNotes app is not running. "
            "Start the MyNotes application and try again."
        )

    # File must be owner-only (600).  We can't enforce this in Python
    # (stat needs a separate call) but we log a warning if the path
    # looks world-readable.
    try:
        mode = runtime_path.stat().st_mode & 0o777
    except OSError:
        raise RuntimeError(
            "Cannot read runtime file permissions. "
            "The MyNotes app may not be running."
        )

    if mode & 0o077:
        logger.warning(
            "Runtime file %s has loose permissions (mode %o); "
            "expected 600",
            runtime_path,
            mode,
        )

    try:
        raw = runtime_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "Cannot read runtime file. The MyNotes app may not be running."
        ) from exc

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Invalid runtime file. The MyNotes app may not be running."
        ) from exc

    url = info.get("url")
    token = info.get("token")
    if not url or not token:
        raise RuntimeError(
            "Incomplete runtime file. The MyNotes app may not be running."
        )

    if not str(url).startswith("http://127.0.0.1"):
        raise RuntimeError(
            "Runtime discovery URL is not a loopback address. "
            "This should not happen."
        )

    return {"url": str(url), "token": str(token)}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _redact_token(url: str, token: str) -> str:
    """Return *url* with the token query parameter redacted."""
    return re.sub(
        r"(token=)[^&]+",
        r"\1***REDACTED***",
        url,
    )


class _HTTPError(Exception):
    """Raised on non-2xx responses from the backend."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


def _api_call(
    method: str,
    path: str,
    runtime: dict[str, str],
    json_body: Any | None = None,
    _transport: httpx.TransferAdapter | None = None,
) -> dict[str, Any]:
    """Make one authenticated request to the MyNotes backend.

    Raises ``_HTTPError`` for non-2xx responses.

    Parameters
    ----------
    _transport:
        Internal test hook.  When provided the transport is passed to
        ``httpx.Client(transport=...)`` instead of creating a network
        client.
    """
    base = runtime["url"].rstrip("/")
    url = base + path

    headers = {
        "Authorization": f"Bearer {runtime['token']}",
        "Content-Type": "application/json",
    }

    # ponytail: fresh httpx client per call — no connection pooling
    # needed for the sporadic MCP tool calls.
    try:
        if _transport is not None:
            with httpx.Client(transport=_transport, timeout=30.0) as client:
                response = client.request(
                    method, url, json=json_body, headers=headers
                )
        else:
            with httpx.Client(timeout=30.0) as client:
                response = client.request(
                    method, url, json=json_body, headers=headers
                )
    except httpx.ConnectError as exc:
        raise RuntimeError(
            "MyNotes app is not running. "
            "Start the MyNotes application and try again."
        ) from exc

    if response.status_code == 409:
        raise _HTTPError(
            409,
            "A conflicting change was detected. "
            "The resource may have been modified by another tool. "
            "Try again with the latest revision.",
        )
    if response.status_code == 401:
        raise RuntimeError(
            "Authentication failed. Please restart the MyNotes app."
        )
    if response.status_code == 404:
        raise _HTTPError(
            404,
            f"Resource not found: {path}. "
            "It may have been deleted.",
        )
    if not response.is_success:
        detail = ""
        try:
            body = response.json()
            detail = body.get("detail", body.get("error", ""))
        except (ValueError, AttributeError):
            detail = response.text[:200]
        raise _HTTPError(
            response.status_code,
            f"Backend request failed ({response.status_code}). "
            f"{detail}".strip(),
        )

    if response.status_code == 204 or response.text == "":
        return {}
    return response.json()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# MCP tools — blocking wrappers called from async @tool functions
# ---------------------------------------------------------------------------

import asyncio


def _tool_get_material(
    document_ids: list[str],
    course_id: str | None = None,
    query: str = "",
    budget: int = 24000,
    _transport: httpx.TransferAdapter | None = None,
) -> dict[str, Any]:
    """Retrieve material from selected documents/courses."""
    runtime = _discover()
    scoped = os.environ.get("MYNOTES_AGENT_DOCUMENT_IDS")
    if scoped is not None:
        selected = json.loads(scoped)
        if any(doc_id not in selected for doc_id in document_ids) or course_id:
            raise RuntimeError("Agent material access is limited to selected document IDs")
        document_ids = document_ids or selected
    result = _api_call(
        "POST",
        "/tools/material",
        runtime,
        json_body={
            "document_ids": document_ids,
            "course_id": course_id,
            "query": query,
            "budget": budget,
        },
        _transport=_transport,
    )
    return result


def _tool_get_deck(
    deck_id: str,
    _transport: httpx.TransferAdapter | None = None,
) -> dict[str, Any]:
    """Read an existing deck and its revision."""
    runtime = _discover()
    return _api_call(
        "GET", f"/tools/decks/{deck_id}", runtime, _transport=_transport
    )


def _tool_save_deck(
    title: str,
    markdown: str,
    source_ids: list[str],
    deck_id: str | None = None,
    expected_revision: int | None = None,
    _transport: httpx.TransferAdapter | None = None,
) -> dict[str, Any]:
    """Create or update a deck."""
    runtime = _discover()
    path = "/tools/decks" if not deck_id else f"/tools/decks/{deck_id}"
    method = "POST" if not deck_id else "PUT"
    body: dict[str, Any] = {
        "title": title,
        "markdown": markdown,
        "source_ids": source_ids,
    }
    if expected_revision is not None:
        body["expected_revision"] = expected_revision
    return _api_call(
        method, path, runtime, json_body=body, _transport=_transport
    )


def _tool_export_deck(
    deck_id: str,
    _transport: httpx.TransferAdapter | None = None,
) -> dict[str, Any]:
    """Queue an HTML/PDF export and return a job ID."""
    runtime = _discover()
    deck = _api_call("GET", f"/tools/decks/{deck_id}", runtime, _transport=_transport)
    return _api_call(
        "POST",
        f"/tools/decks/{deck_id}/exports",
        runtime,
        json_body={"revision": deck["revision"]},
        _transport=_transport,
    )


def _tool_get_job(
    job_id: str,
    _transport: httpx.TransferAdapter | None = None,
) -> dict[str, Any]:
    """Get the status of a background job."""
    runtime = _discover()
    return _api_call(
        "GET", f"/tools/jobs/{job_id}", runtime, _transport=_transport
    )


# ---------------------------------------------------------------------------
# MCP server setup
# ---------------------------------------------------------------------------

def _build_server() -> Any:
    """Create and configure the MCP server with all tools."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="mynotes")

    @server.tool()
    def list_allowed_files(directory: str = "", pattern: str = "*") -> str:
        """List files inside directories explicitly allowed in MyNotes Settings."""
        from mynotes.agent_files import list_files
        return json.dumps(list_files(directory, pattern))

    @server.tool()
    def read_allowed_file(path: str, offset: int = 1, limit: int = 200) -> str:
        """Read allowed file text/PDF with line or page provenance. No credential access."""
        from mynotes.agent_files import read_file
        return json.dumps(read_file(path, offset, limit), ensure_ascii=False)

    @server.tool()
    def search_allowed_files(query: str, directory: str = "") -> str:
        """Search text in allowed directories; bounded lexical results, no embeddings."""
        from mynotes.agent_files import search_files
        return json.dumps(search_files(query, directory), ensure_ascii=False)

    @server.tool()
    async def search_web(query: str) -> str:
        """Search the public Internet. Returns up to five URLs/snippets or an honest availability warning."""
        from mynotes.web_tools import search_web as search
        return json.dumps(await asyncio.to_thread(search, query), ensure_ascii=False)

    @server.tool()
    async def read_web(url: str) -> str:
        """Read a public HTTPS page for evidence. No login, local/private URLs or credentials; cite the returned URL."""
        from mynotes.web_tools import read_web as read
        return json.dumps(await asyncio.to_thread(read, url), ensure_ascii=False)

    @server.tool()
    async def get_material(
        document_ids: list[str],
        course_id: str | None = None,
        query: str = "",
        budget: int = 24000,
    ) -> str:
        """Retrieve material from selected documents or a course."""
        try:
            result = await asyncio.to_thread(
                _tool_get_material,
                document_ids=document_ids,
                course_id=course_id,
                query=query,
                budget=budget,
            )
        except _HTTPError as exc:
            return f"Error: {exc.message}"
        except RuntimeError as exc:
            return f"Error: {exc.args[0]}"
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool()
    async def get_deck(deck_id: str) -> str:
        """Read an existing slide deck and its revision."""
        try:
            result = await asyncio.to_thread(_tool_get_deck, deck_id=deck_id)
        except _HTTPError as exc:
            return f"Error: {exc.message}"
        except RuntimeError as exc:
            return f"Error: {exc.args[0]}"
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool()
    async def save_deck(
        title: str,
        markdown: str,
        source_ids: list[str],
        deck_id: str | None = None,
        expected_revision: int | None = None,
    ) -> str:
        """Create or update a slide deck (Marp Markdown)."""
        try:
            result = await asyncio.to_thread(
                _tool_save_deck,
                title=title,
                markdown=markdown,
                source_ids=source_ids,
                deck_id=deck_id,
                expected_revision=expected_revision,
            )
        except _HTTPError as exc:
            return f"Error: {exc.message}"
        except RuntimeError as exc:
            return f"Error: {exc.args[0]}"
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool()
    async def export_deck(deck_id: str) -> str:
        """Queue an HTML/PDF export for a saved deck."""
        try:
            result = await asyncio.to_thread(_tool_export_deck, deck_id=deck_id)
        except _HTTPError as exc:
            return f"Error: {exc.message}"
        except RuntimeError as exc:
            return f"Error: {exc.args[0]}"
        return json.dumps(result, ensure_ascii=False, indent=2)

    @server.tool()
    async def get_job(job_id: str) -> str:
        """Get the status of a background export or import job."""
        try:
            result = await asyncio.to_thread(_tool_get_job, job_id=job_id)
        except _HTTPError as exc:
            return f"Error: {exc.message}"
        except RuntimeError as exc:
            return f"Error: {exc.args[0]}"
        return json.dumps(result, ensure_ascii=False, indent=2)

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the MCP bridge over stdio."""
    server = _build_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
