"""MyNotes CLI entry point.

Reads stdin JSON for credentials, acquires OS flock to prevent duplicate
instances, starts Uvicorn on loopback port 0, writes runtime discovery
file, prints readiness JSON to stdout.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import pathlib
import signal
import socket
import sys

from uvicorn import Config, Server

from mynotes.api import create_app
from mynotes.db import Database
from mynotes.runtime import RuntimeFile
from mynotes.security import CredentialStore

logger = logging.getLogger("mynotes")

# Ensure src/ is on path when run as module from repo root
_pkg = pathlib.Path(__file__).resolve().parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

DEFAULT_DATA_DIR = pathlib.Path.home() / ".local" / "share" / "mynotes"
DEFAULT_RUNTIME_FILE = (
    pathlib.Path.home() / ".local" / "state" / "mynotes" / "runtime.json"
)
_LOCK_FILE = ".mynotes.lock"
_global_lockfd = None


def _acquire_lock(data_dir: pathlib.Path) -> None:
    """Exclusive non-blocking flock; file persists on disk after release."""
    lock_path = data_dir / _LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    global _global_lockfd
    try:
        fd = open(lock_path, "w")  # noqa: SIM115  # fd kept open for _release_lock
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        _global_lockfd = fd
    except OSError:
        with contextlib.suppress(Exception):
            fd.close()
        logger.error("Cannot acquire lock at %s", lock_path)
        sys.exit(1)


def _release_lock() -> None:
    """Release flock and close fd.  Never unlinks the lock file."""
    global _global_lockfd
    if _global_lockfd is not None:
        try:
            import fcntl

            fcntl.flock(_global_lockfd, fcntl.LOCK_UN)
            _global_lockfd.close()
        except OSError:
            pass
        _global_lockfd = None


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    """Convert SIGTERM → KeyboardInterrupt so ``finally`` in ``main()`` runs."""
    raise KeyboardInterrupt()


class _ReadyServer(Server):
    """Uvicorn server that prints readiness after lifespan startup."""

    _runtime_file: RuntimeFile
    _app_token: str
    _tool_token: str

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        url = None
        for srv in self.servers:
            for raw_sock in srv.sockets:
                addr = raw_sock.getsockname()
                url = f"http://{addr[0]}:{addr[1]}"
                break
            if url:
                break
        if url is None:
            return
        self._runtime_file.write(url, self._tool_token)
        readiness = {
            "url": url,
            "token": self._app_token,
            "pid": os.getpid(),
        }
        print(json.dumps(readiness), flush=True)


def main() -> None:
    """CLI entry point: parse args, read stdin, start server, handle signals."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info("MyNotes starting")

    parser = argparse.ArgumentParser(prog="mynotes")
    subparsers = parser.add_subparsers(dest="command")
    serve_p = subparsers.add_parser("serve")
    serve_p.add_argument("--port", type=int, default=0, help="Port to bind (0=random)")
    serve_p.add_argument(
        "--data-dir", type=str, default=str(DEFAULT_DATA_DIR),
        help="Data directory path",
    )
    subparsers.add_parser("mcp")
    subparsers.add_parser("diarize")
    args = parser.parse_args()

    if args.command == "diarize":
        from mynotes.diarization import child_main
        child_main()
        return

    if args.command == "mcp":
        from mynotes.bridge import main as bridge_main
        bridge_main()
        return

    if args.command != "serve":
        parser.print_help()
        sys.exit(0)

    # --- read stdin (credentials from Electron) -------------------------
    try:
        raw = sys.stdin.readline()
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid stdin JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    token: str = config.get("token", "")
    tool_token: str = config.get("tool_token", "")
    fernet_key: str | None = config.get("fernet_key")

    # --- resolve paths --------------------------------------------------
    data_dir = pathlib.Path(args.data_dir)
    runtime_path = pathlib.Path(
        os.environ.get("MYNOTES_RUNTIME_FILE", str(DEFAULT_RUNTIME_FILE))
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)

    # --- OS-level single-instance lock ----------------------------------
    _acquire_lock(data_dir)

    # --- build app ------------------------------------------------------
    db = Database(data_dir / "library.sqlite3")
    credentials = CredentialStore(data_dir, fernet_key)
    runtime_file = RuntimeFile(runtime_path)

    app = create_app(
        db=db, data_dir=data_dir, token=token, tool_token=tool_token,
        credentials=credentials, runtime_file=runtime_path,
    )

    # --- start server ---------------------------------------------------
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", args.port))
        sock.listen(1)
        port = sock.getsockname()[1]

        cfg = Config(
            app, host="127.0.0.1", port=port, log_level="info",
        )
        server = _ReadyServer(config=cfg)
        server._runtime_file = runtime_file
        server._app_token = token
        server._tool_token = tool_token
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Cleaning up")
        runtime_file.remove()
        _release_lock()
        logger.info("MyNotes stopped")


if __name__ == "__main__":
    main()
