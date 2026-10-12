import asyncio
from unittest.mock import MagicMock, patch

import pytest

from mynotes.bridge import _build_server
from mynotes.web_tools import _PinnedHTTPSConnection, make_request, research_web, search_web


def test_query_relative_redirects_and_validated_pinned_connection():
    connections = []
    def connection(host, ip, **kwargs):
        conn = MagicMock()
        connections.append(conn)
        response = conn.getresponse.return_value
        response.status = 302 if len(connections) == 1 else 200
        response.getheaders.return_value = [("location", "/next?q=retained")] if response.status == 302 else [("content-type", "text/plain")]
        response.read.return_value = b"Evidence"
        assert host == "example.com" and ip == "93.184.216.34"
        return conn
    with patch("mynotes.web_tools.get_global_dns", return_value=["93.184.216.34"]), patch("mynotes.web_tools._PinnedHTTPSConnection", side_effect=connection):
        assert make_request("https://example.com/search?q=actual") == "Evidence"
    assert connections[0].request.call_args.args == ("GET", "/search?q=actual")
    assert connections[1].request.call_args.args == ("GET", "/next?q=retained")
    assert all(c.close.called for c in connections)


def test_private_redirect_rejected_and_body_limit_enforced():
    conn = MagicMock()
    response = conn.getresponse.return_value
    response.status = 302
    response.getheaders.return_value = [("Location", "https://127.0.0.1/admin")]
    def dns(host):
        if host != "example.com":
            raise ValueError("non-global address")
        return ["93.184.216.34"]
    with patch("mynotes.web_tools.get_global_dns", side_effect=dns), patch("mynotes.web_tools._PinnedHTTPSConnection", return_value=conn):
        with pytest.raises(ValueError, match="non-global"):
            make_request("https://example.com")
        response.status = 200
        response.getheaders.return_value = [("Content-Type", "text/plain")]
        response.read.return_value = b"x" * 2_000_001
        with pytest.raises(ValueError, match="2 MB"):
            make_request("https://example.com")


def test_tls_uses_hostname_while_socket_uses_validated_ip():
    sock = MagicMock()
    context = MagicMock()
    with patch("mynotes.web_tools.socket.create_connection", return_value=sock) as connect, patch("mynotes.web_tools.ssl.create_default_context", return_value=context):
        _PinnedHTTPSConnection("example.com", "93.184.216.34").connect()
    assert connect.call_args.args[0] == ("93.184.216.34", 443)
    context.wrap_socket.assert_called_once_with(sock, server_hostname="example.com")


def test_search_fallback_and_mcp_page_reading():
    responses = ["<html>captcha</html>", "<rss><channel><item><title>Public evidence</title><link>https://example.com</link><description>A source</description></item></channel></rss>"]
    with patch("mynotes.web_tools.make_request", side_effect=responses):
        result = search_web("public topic")
    assert result["provider"] == "Bing RSS"
    assert result["results"][0]["title"] == "Public evidence"
    with patch("mynotes.web_tools.make_request", return_value="<html><title>Evidence</title><h1>Public topic</h1><p>Sourced fact</p></html>"):
        tool = asyncio.run(_build_server().call_tool("read_web", {"url": "https://example.com"}))
    assert "Sourced fact" in str(tool) and "https://example.com" in str(tool)


def test_research_reads_requested_url_and_reports_unavailable_evidence():
    with patch("mynotes.web_tools.search_web") as search, patch("mynotes.web_tools.read_web", return_value={"url": "https://example.com", "title": "Evidence", "text": "Actual public fact", "truncated": False}) as read:
        material = research_web("Read https://example.com.")
    search.assert_not_called()
    read.assert_called_once_with("https://example.com")
    assert material["blocks"][0]["source_id"] == "https://example.com"
    assert material["blocks"][0]["text"] == "Actual public fact"
    with patch("mynotes.web_tools.search_web", return_value={"results": [], "warnings": ["Blocked"]}):
        with pytest.raises(ValueError, match="Blocked"):
            research_web("Find a public fact")


def test_web_chat_fetches_evidence_even_without_agent_harness(tmp_path):
    import json
    from mynotes.db import Database
    from mynotes.worker import Worker
    db = Database(tmp_path / "library.sqlite3")
    credentials = MagicMock()
    credentials.get.return_value = "placeholder"
    worker = Worker(db=db, data_dir=tmp_path, credentials=credentials)
    thread = db.create_conversation()
    provider = MagicMock()
    provider.chat.return_value = {"answer": "Fact [source:https://example.com]", "citations": [{"source_id": "https://example.com"}], "warnings": []}
    material = {"mode": "web", "metadata": {}, "warnings": [], "blocks": [{"source_id": "https://example.com", "text": "Actual fact", "title": "Public source", "locator": {"url": "https://example.com"}}]}
    with patch("mynotes.web_tools.research_web", return_value=material) as research, patch.object(worker, "_provider", return_value=provider):
        worker.schedule("chat", {"mode": "web", "question": "Public question", "conversation_id": thread["id"], "harness": True})
        job = worker.run_once()
    assert job["status"] == "succeeded"
    research.assert_called_once_with("Public question")
    assert provider.chat.call_args.kwargs["material"] == material
    result = json.loads(job["result"])
    assert result["citations"][0]["source_id"] == "https://example.com"
    assert db.list_chats(thread["id"])[0]["mode"] == "web"
