"""Tests for src/mynotes/runtime.py — RuntimeFile atomic write/read/remove.

Subprocess CLI serve — real startup, loopback health check, runtime
file written then cleaned up on terminate.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from mynotes.runtime import RuntimeFile


@pytest.fixture()
def data_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Unique per-test data directory."""
    d = tmp_path / "data"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Atomic write / read
# ---------------------------------------------------------------------------


class TestAtomicWriteRead:
    def test_write_and_read(self, data_dir: pathlib.Path) -> None:
        path = data_dir / "runtime.json"
        rf = RuntimeFile(path)
        rf.write(url="http://127.0.0.1:8765", tool_token="secret-tool-token")
        data = rf.read()
        assert data is not None
        assert data["url"] == "http://127.0.0.1:8765"
        assert data["token"] == "secret-tool-token"
        assert data["pid"] == os.getpid()

    def test_write_mode_600(self, data_dir: pathlib.Path) -> None:
        path = data_dir / "runtime.json"
        rf = RuntimeFile(path)
        rf.write(url="http://x", tool_token="x")
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_no_transient_tmp(self, data_dir: pathlib.Path) -> None:
        """Random temp names with O_EXCL leave no stale temp files."""
        path = data_dir / "runtime.json"
        rf = RuntimeFile(path)
        rf.write(url="http://x", tool_token="x")
        tmp_files = list(data_dir.glob("*.tmp.*"))
        assert tmp_files == [], f"Transient temp files found: {tmp_files}"

    def test_read_corrupt_json(self, data_dir: pathlib.Path) -> None:
        path = data_dir / "bad.json"
        path.write_text("not json {{{")
        assert RuntimeFile(path).read() is None

    def test_read_empty_file(self, data_dir: pathlib.Path) -> None:
        path = data_dir / "empty.json"
        path.write_text("")
        assert RuntimeFile(path).read() is None

    def test_schema_fields(self, data_dir: pathlib.Path) -> None:
        path = data_dir / "runtime.json"
        rf = RuntimeFile(path)
        rf.write(url="http://127.0.0.1:5555", tool_token="schema-token")
        data = rf.read()
        assert all(k in data for k in ("url", "token", "pid"))
        assert isinstance(data["pid"], int)
        assert data["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# Token ownership
# ---------------------------------------------------------------------------


class TestTokenOwnership:
    def test_remove_own_entry(self, data_dir: pathlib.Path) -> None:
        path = data_dir / "runtime.json"
        rf = RuntimeFile(path)
        rf.write(url="http://127.0.0.1:1234", tool_token="own-token")
        rf.remove()
        assert rf.read() is None

    def test_remove_preserves_on_pid_mismatch(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "runtime.json"

        # Create entry with our PID (default)
        rf_owner = RuntimeFile(path)
        rf_owner.write(url="http://127.0.0.1:5678", tool_token="token2")
        assert rf_owner.read() is not None

        # Remove with a different PID → should NOT remove
        rf_other = RuntimeFile(path, owner_token="999999")
        rf_other.remove()  # pid mismatch → no-op
        assert rf_owner.read() is not None  # entry still exists

        # Now remove with correct PID
        rf_owner.remove()
        assert rf_owner.read() is None


# ---------------------------------------------------------------------------
# Environment variable
# ---------------------------------------------------------------------------


class TestEnvironmentVariable:
    def test_runtime_file_env_override(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom_path = tmp_path / "custom" / "runtime.json"
        monkeypatch.setenv("MYNOTES_RUNTIME_FILE", str(custom_path))
        rf = RuntimeFile(None)
        assert rf.path == custom_path
        rf.write(url="http://127.0.0.1:9999", tool_token="env-token")
        data = rf.read()
        assert data["url"] == "http://127.0.0.1:9999"


# ---------------------------------------------------------------------------
# Subprocess CLI serve
# ---------------------------------------------------------------------------


def _launch_server(
    data_dir: pathlib.Path,
    runtime_path: pathlib.Path,
    app_token: str,
    tool_token: str,
):
    """Launch a server subprocess and return (proc, readiness)."""
    stdin_payload = json.dumps({
        "token": app_token,
        "tool_token": tool_token,
        "fernet_key": None,
    })

    env = os.environ.copy()
    env["MYNOTES_RUNTIME_FILE"] = str(runtime_path)
    env["PYTHONPATH"] = str(pathlib.Path(__file__).parent.parent / "src")

    proc = subprocess.Popen(
        [sys.executable, "-m", "mynotes", "serve",
         "--port", "0", "--data-dir", str(data_dir)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env,
    )
    proc.stdin.write(stdin_payload + "\n")
    proc.stdin.flush()

    # Wait for readiness JSON on stdout
    for _ in range(40):
        line = proc.stdout.readline()
        if "token" in line:
            return proc, json.loads(line)
        time.sleep(0.1)

    proc.terminate()
    proc.wait(timeout=10)
    pytest.fail("server did not print readiness JSON")


def _cleanup(proc):
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


class TestCLIServe:
    """End-to-end: launch via sys.executable, wait for readiness,
    verify loopback health, check runtime file, terminate, verify cleanup."""

    def test_serve_start_stop_runtime_cleanup(
        self, data_dir: pathlib.Path, request: pytest.FixtureRequest
    ) -> None:
        runtime_path = data_dir / "runtime.json"
        app_token = "app-token-123"
        tool_token = "tool-token-456"

        proc, readiness = _launch_server(
            data_dir, runtime_path, app_token, tool_token
        )
        request.addfinalizer(lambda: _cleanup(proc))

        # Verify readiness JSON contains APP token (for Electron frontend)
        assert "url" in readiness
        assert readiness["token"] == app_token

        # Extract port from readiness URL
        port = int(readiness["url"].split(":")[2])

        # Health check — loopback HTTP with app token auth on /health
        import httpx

        r = httpx.get(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {app_token}"},
            timeout=2,
        )
        assert r.status_code == 200

        # Verify runtime file was written (tool_token for MCP bridge)
        time.sleep(0.2)
        assert runtime_path.exists()
        entry = json.loads(runtime_path.read_text())
        assert entry["token"] == tool_token
        assert entry["url"] == readiness["url"]

        # Shutdown
        proc.terminate()
        proc.wait(timeout=10)

        # Verify runtime discovery is absent after shutdown
        for _ in range(20):
            if not runtime_path.exists():
                break
            time.sleep(0.1)
        assert not runtime_path.exists(), "runtime file not cleaned up after shutdown"

    def test_tool_auth_material(
        self, data_dir: pathlib.Path, request: pytest.FixtureRequest
    ) -> None:
        """Verify MCP tool endpoint requires tool_token auth."""
        runtime_path = data_dir / "runtime.json"
        app_token = "app-token-abc"
        tool_token = "tool-token-xyz"

        proc, readiness = _launch_server(
            data_dir, runtime_path, app_token, tool_token
        )
        request.addfinalizer(lambda: _cleanup(proc))

        port = int(readiness["url"].split(":")[2])

        import httpx

        # Tool endpoint with wrong token → 403
        r = httpx.post(
            f"http://127.0.0.1:{port}/tools/material",
            headers={"Authorization": "Bearer wrong-token"},
            json={},
            timeout=2,
        )
        assert r.status_code == 403

        # Tool endpoint with correct tool_token → 200
        r = httpx.post(
            f"http://127.0.0.1:{port}/tools/material",
            headers={"Authorization": f"Bearer {tool_token}"},
            json={},
            timeout=2,
        )
        assert r.status_code == 200

        proc.terminate()
        proc.wait(timeout=10)
