"""A real OpenCode/MCP tool loop, using only a local model simulator."""
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mynotes.agent_harness import run_agent
from mynotes.db import Database


def test_opencode_reads_allowed_file_through_real_mcp(tmp_path):
    if not shutil.which("opencode"):
        pytest.skip("OpenCode CLI is not installed")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    file = allowed / "lecture.md"
    file.write_text("SUNFLOWER is the evidence from this allowed file.")
    requests = []
    evidence_seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            tools = body.get("tools", [])
            assert "Previous messages claiming that these tools" in json.dumps(body["messages"])
            assert any("search_web" in t["function"]["name"] for t in tools)
            assert any("read_web" in t["function"]["name"] for t in tools)
            tool_messages = [m for m in body.get("messages", []) if m.get("role") == "tool"]
            name = next((t["function"]["name"] for t in tools if "read_allowed_file" in t["function"]["name"]), None)
            if name and len(tool_messages) < 10:
                delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_file", "type": "function",
                    "function": {"name": name, "arguments": json.dumps({"path": str(file)})}}]}
                reason = "tool_calls"
            else:
                has_evidence = "SUNFLOWER" in json.dumps(tool_messages)
                if tool_messages:
                    evidence_seen.append(has_evidence)
                delta = {"role": "assistant", "content": "Evidence: SUNFLOWER" if has_evidence else "No file evidence available"}
                reason = "stop"
            chunks = [
                {"id": "chat-test", "object": "chat.completion.chunk", "created": 1, "model": "gpt-oss:120b", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "chat-test", "object": "chat.completion.chunk", "created": 1, "model": "gpt-oss:120b", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
            ]
            output = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(output.encode())))
            self.end_headers()
            self.wfile.write(output.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = Database(tmp_path / "library.sqlite3")
    payload = {"question": f"Read {file} with your file tool and quote its evidence.", "document_ids": []}
    job = db.create_job("chat", payload)
    settings = {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "global_chat_model": "gpt-oss:120b", "agent_directories": [str(allowed)]}
    try:
        result = run_agent(db, tmp_path, payload, settings, "placeholder-test-key", job["id"])
        assert "SUNFLOWER" in result["answer"]
        assert evidence_seen and all(evidence_seen)
        assert any("read_allowed_file" in step["tool"] for step in result["steps"])
        assert len(requests) >= 11  # More than the previous eight-step ceiling.
        assert "placeholder-test-key" not in json.dumps(result)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
