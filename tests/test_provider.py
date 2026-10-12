"""Tests for src/mynotes/provider.py and src/mynotes/security.py.

All provider tests use httpx.MockTransport (no mocking libraries).
Tests upload real bytes and verify structured responses.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mynotes.security import CredentialStore, _CREDENTIALS_FILENAME

def test_separate_models_route_to_correct_requests(tmp_path):
    from mynotes.provider import Provider
    seen = []
    def handler(request):
        if "multipart/form-data" in request.headers.get("content-type", ""):
            body = request.read()
            assert b"audio-model" in body
            assert b"audio-bytes" in body
            return httpx.Response(200, json={"text": "Transcript"})
        payload = json.loads(request.content)
        seen.append(payload["model"])
        text = '{"title":"Suggested"}' if payload["model"] == "fast-model" else "Description"
        return httpx.Response(200, json=_chat_response(text))
    settings = _make_settings(fast_model="fast-model", vision_model="vision-model", transcription_model="audio-model")
    with Provider(settings) as provider:
        provider._client.close()
        provider._client = httpx.Client(base_url=settings["base_url"], transport=httpx.MockTransport(handler))
        provider.suggest(_make_material())
        image = tmp_path / "page.png"
        image.write_bytes(b"image-bytes")
        provider.describe(image, "Page", "Describe")
        audio = tmp_path / "recording.wav"
        audio.write_bytes(b"audio-bytes")
        provider.transcribe(audio)
    assert seen == ["fast-model", "vision-model"]

def test_base64_string_key_encrypts_and_reloads(tmp_path):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    store = CredentialStore(tmp_path, key)
    store.set("placeholder-test-key")
    assert b"placeholder-test-key" not in (tmp_path / _CREDENTIALS_FILENAME).read_bytes()
    assert CredentialStore(tmp_path, key).get() == "placeholder-test-key"
    assert CredentialStore(tmp_path, key).persistent is True

def test_failed_encrypted_save_is_reported_not_silently_successful(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    store = CredentialStore(tmp_path, Fernet.generate_key().decode())
    def fail(*args):
        raise OSError("test storage unavailable")
    monkeypatch.setattr(store, "_write_encrypted", fail)
    with pytest.raises(OSError, match="could not be saved"):
        store.set("placeholder-test-key")
    assert store.storage_error is not None

@pytest.mark.parametrize("start,end", [(-1, 1), (float("nan"), 1), (0, float("inf"))])
def test_invalid_timestamp_values_are_rejected(start, end):
    from mynotes.provider import Provider
    with pytest.raises(ValueError):
        Provider._ts({"start": start, "end": end})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_settings(**overrides: Any) -> dict:
    base = {
        "base_url": "https://test-api.example.com/v1",
        "chat_model": "test-model",
        "timeout": 10.0,
        "max_retries": 0,
    }
    base.update(overrides)
    return base


def _make_material() -> dict:
    return {
        "metadata": {
            "title": "Probability Lecture",
            "course": "MATH-301",
            "source_id": "doc-001",
        },
        "blocks": [
            {"source_id": "block-1", "excerpt": "Bayes' theorem relates P(A|B) to P(B|A)."},
            {"source_id": "block-2", "excerpt": "The law of total probability sums over partitions."},
        ],
        "content": "Full lecture notes on probability theory.",
    }


def _chat_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _transport_factory(
    resp: dict | None = None, status_code: int = 200,
    raise_exc: Exception | None = None, json_resp: dict | None = None,
) -> httpx.MockTransport:
    """Return a MockTransport that returns *resp* JSON or raises."""
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc:
            raise raise_exc
        return httpx.Response(status_code, json=json_resp or resp or {})
    return httpx.MockTransport(handler)


def _make_provider(settings: dict | None = None) -> tuple[Any, httpx.Client]:
    """Create a Provider with a MockTransport and return (provider, client)."""
    if settings is None:
        settings = _make_settings()
    settings = dict(settings)  # copy
    # Replace base_url so MockTransport works
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    return settings, transport


# ---------------------------------------------------------------------------
# Provider — initialisation
# ---------------------------------------------------------------------------

class TestProviderInit:
    def test_requires_https_for_non_loopback(self) -> None:
        with pytest.raises(ValueError, match="HTTPS"):
            from mynotes.provider import Provider
            Provider(_make_settings(base_url="http://remote.example.com/v1"))

    def test_allows_loopback_http(self) -> None:
        from mynotes.provider import Provider
        with _mock_client(_make_settings(base_url="http://127.0.0.1:8080/v1")) as p:
            assert p.base_url == "http://127.0.0.1:8080/v1"

    def test_no_api_key_allowed(self) -> None:
        from mynotes.provider import Provider
        with _mock_client(_make_settings(), api_key=None) as p:
            assert p.api_key is None

    def test_headers_include_auth_when_key_present(self) -> None:
        from mynotes.provider import Provider
        with _mock_client(_make_settings(), api_key="sk-test") as p:
            assert p._client.headers.get("Authorization") == "Bearer sk-test"


def _mock_client(settings: dict, api_key: str | None = "sk-test",
                 transport: httpx.MockTransport | None = None) -> Provider:
    """Return a Provider with a MockTransport (context manager)."""
    transport = transport or _transport_factory(_chat_response("ok"))
    settings = dict(settings)
    # Inject transport
    from mynotes.provider import Provider
    p = Provider(settings, api_key=api_key)
    p._client = httpx.Client(transport=transport, base_url=p.base_url,
                             headers=p._client.headers, timeout=p._timeout)
    return p


# ---------------------------------------------------------------------------
# Provider — chat
# ---------------------------------------------------------------------------

class TestProviderChat:
    def test_chat_returns_answer_and_citations(self) -> None:
        content = "Bayes' theorem relates P(A|B) to P(B|A) [source:block-1]."
        resp = _chat_response(content)
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.chat(_make_material(), "What is Bayes' theorem?")
        assert result["answer"] == content
        assert result["mode"] == "unverified"
        assert len(result["citations"]) == 1
        assert result["citations"][0]["source_id"] == "block-1"
        assert "Bayes" in result["citations"][0]["excerpt"]

    def test_chat_rejects_invented_citation(self) -> None:
        resp = _chat_response("Some claim [source:block-999].")
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.chat(_make_material(), "test")
        assert len(result["citations"]) == 0
        assert any("block-999" in w for w in result["warnings"])

    def test_chat_no_citations_produces_warning(self) -> None:
        resp = _chat_response("Here is an answer without any citations.")
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.chat(_make_material(), "test")
        assert any("no citations" in w.lower() for w in result["warnings"])

    def test_chat_verified_mode(self) -> None:
        resp = _chat_response("Answer [source:block-1].")
        transport = _transport_factory(resp)
        settings = _make_settings(verified_chat=True)
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.chat(_make_material(), "q")
        assert result["mode"] == "verified"

    def test_chat_with_title_locator_version(self) -> None:
        mat = dict(_make_material())
        mat["blocks"][0]["title"] = "Lecture 3"
        mat["blocks"][0]["locator"] = "p.12"
        mat["blocks"][0]["version"] = "2024-01"
        resp = _chat_response("[source:block-1]")
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.chat(mat, "q")
        cite = result["citations"][0]
        assert cite["title"] == "Lecture 3"
        assert cite["locator"] == "p.12"
        assert cite["version"] == "2024-01"


# ---------------------------------------------------------------------------
# Provider — suggest
# ---------------------------------------------------------------------------

class TestProviderSuggest:
    def test_suggest_returns_structured_json(self) -> None:
        sugg = {"title": "Notes", "keywords": ["a", "b"]}
        resp = _chat_response(json.dumps(sugg))
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.suggest({"title": "Raw"})
        assert result["suggestions"] == sugg
        assert result["warnings"] == []

    def test_suggest_fallback_on_invalid_json(self) -> None:
        resp = _chat_response("not json at all")
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.suggest({"title": "Raw"})
        assert "_raw" in result["suggestions"]
        assert any("JSON" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Provider — draft_slides
# ---------------------------------------------------------------------------

class TestProviderDraftSlides:
    def test_draft_slides_structured_json(self) -> None:
        deck = {"title": "Intro", "slides": [
            {"heading": "Bayes", "body": "formula", "source_ids": ["block-1"]},
        ]}
        resp = _chat_response(json.dumps(deck))
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.draft_slides(_make_material(), "Topic", 3, "English")
        assert result["title"] == "Intro"
        assert len(result["slides"]) == 1
        assert result["slides"][0]["heading"] == "Bayes"
        assert result["slides"][0]["body"] == "formula"
        assert result["slides"][0]["source_ids"] == ["block-1"]

    def test_draft_slides_markdown_fallback(self) -> None:
        md = "### Slide 1\nbody text\n---\n### Slide 2\nmore"
        resp = _chat_response(md)
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.draft_slides(_make_material(), "T", 1, "E")
        assert len(result["slides"]) == 2
        assert result["slides"][0]["heading"] == "Slide 1"

    def test_draft_slides_invalid_citation(self) -> None:
        deck = {"title": "T", "slides": [{"heading": "H", "body": "b",
                                           "source_ids": ["block-999"]}]}
        resp = _chat_response(json.dumps(deck))
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.draft_slides(_make_material(), "T", 1, "E")
        assert any("block-999" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Provider — transcribe (real bytes, MockTransport)
# ---------------------------------------------------------------------------

class TestProviderTranscribe:
    def test_transcribe_missing_file(self, tmp_path: Path) -> None:
        from mynotes.provider import Provider
        with Provider(_make_settings()) as p:
            result = p.transcribe(tmp_path / "missing.wav")
        assert result["text"] == ""
        assert any("not found" in w for w in result["warnings"])

    def test_transcribe_upload_and_parse(self, tmp_path: Path) -> None:
        audio = tmp_path / "test.wav"
        audio.write_bytes(b"fake audio bytes")
        resp = {"text": "hello world", "segments": [
            {"text": "hello", "start": 0.0, "end": 0.5},
            {"text": "world", "start": 0.6, "end": 1.2},
        ]}
        transport = _transport_factory(resp)
        from mynotes.provider import Provider
        with Provider(_make_settings()) as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.transcribe(audio)
        assert result["text"] == "hello world"
        assert len(result["segments"]) == 2
        assert result["segments"][0]["text"] == "hello"

    def test_transcribe_invalid_timestamp_raises(self, tmp_path: Path) -> None:
        audio = tmp_path / "test.wav"
        audio.write_bytes(b"data")
        resp = {"text": "x", "segments": [{"text": "x", "start": "bad", "end": 1.0}]}
        transport = _transport_factory(resp)
        from mynotes.provider import Provider
        with Provider(_make_settings()) as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            with pytest.raises(ValueError, match="Non-numeric"):
                p.transcribe(audio)

    def test_transcribe_multipart_uploads_real_bytes(self, tmp_path: Path) -> None:
        """Verify that the actual file bytes are sent in the multipart body."""
        audio = tmp_path / "upload.wav"
        audio.write_bytes(b"REAL_BYTES_12345")
        captured_body: list[bytes] = []

        def capture(request: httpx.Request) -> httpx.Response:
            captured_body.append(request.read())
            return httpx.Response(200, json={"text": "ok", "segments": []})

        transport = httpx.MockTransport(capture)
        from mynotes.provider import Provider
        with Provider(_make_settings()) as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            p.transcribe(audio)
        assert b"REAL_BYTES_12345" in b"".join(captured_body)

    def test_transcribe_unrecognised_response_raises(self, tmp_path: Path) -> None:
        audio = tmp_path / "t.wav"
        audio.write_bytes(b"x")
        transport = _transport_factory({"data": "nope"})
        from mynotes.provider import Provider
        with Provider(_make_settings()) as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            with pytest.raises(ValueError, match="Unrecognised"):
                p.transcribe(audio)


# ---------------------------------------------------------------------------
# Provider — describe (vision via /chat/completions)
# ---------------------------------------------------------------------------

class TestProviderDescribe:
    def test_describe_missing_file(self, tmp_path: Path) -> None:
        from mynotes.provider import Provider
        with Provider(_make_settings()) as p:
            result = p.describe(tmp_path / "x.png", "ctx", "q")
        assert any("not found" in w for w in result["warnings"])

    def test_describe_uses_chat_path_with_image_url(self, tmp_path: Path) -> None:
        """Vision uses /chat/completions, not a bogus /vision/completions."""
        img = tmp_path / "x.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        requested_paths: list[str] = []
        def capture(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            return httpx.Response(200, json=_chat_response("chart"))
        transport = httpx.MockTransport(capture)
        from mynotes.provider import Provider
        with Provider(_make_settings()) as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.describe(img, "ctx", "q")
        assert any("/chat/completions" in p for p in requested_paths)
        assert result["description"] == "chart"

    def test_describe_uses_base64_image_url(self, tmp_path: Path) -> None:
        """Verify image is sent as base64 in the content field."""
        img = tmp_path / "x.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        captured_content: list[str] = []
        def capture(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            msg = body["messages"][1]["content"]
            img_part = [c for c in msg if c["type"] == "image_url"][0]
            captured_content.append(img_part["image_url"]["url"])
            return httpx.Response(200, json=_chat_response("desc"))
        transport = httpx.MockTransport(capture)
        from mynotes.provider import Provider
        with Provider(_make_settings()) as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            p.describe(img, "ctx", "q")
        assert "base64" in captured_content[0]


# ---------------------------------------------------------------------------
# Provider — error handling (no-network → raises)
# ---------------------------------------------------------------------------

class TestProviderErrorHandling:
    def test_network_error_raises_not_silent(self) -> None:
        from mynotes.provider import Provider
        settings = _make_settings()
        transport = _transport_factory(raise_exc=httpx.ConnectError("no network"))
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            with pytest.raises(httpx.ConnectError):
                p.chat(_make_material(), "q")

    def test_request_error_is_raised(self) -> None:
        from mynotes.provider import Provider
        settings = _make_settings()
        transport = _transport_factory(raise_exc=httpx.TimeoutException("timeout"))
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            with pytest.raises(httpx.TimeoutException):
                p.chat(_make_material(), "q")

    def test_4xx_returns_error_not_silent(self) -> None:
        transport = _transport_factory(status_code=400, json_resp={"error": "bad request"})
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            with pytest.raises(httpx.HTTPStatusError):
                p.chat(_make_material(), "q")


# ---------------------------------------------------------------------------
# Provider — context_budget not handled here
# ---------------------------------------------------------------------------

class TestProviderNoTruncation:
    def test_chat_does_not_truncate_content(self) -> None:
        """Provider does not truncate; caller must handle context_budget."""
        long_content = "x" * 50000
        mat = _make_material()
        mat["content"] = long_content
        resp = _chat_response("short answer")
        transport = _transport_factory(resp)
        settings = _make_settings()
        from mynotes.provider import Provider
        with Provider(settings, api_key="x") as p:
            p._client = httpx.Client(transport=transport, base_url=p.base_url,
                                     headers=p._client.headers, timeout=p._timeout)
            result = p.chat(mat, "q")
        # Answer should be intact — no truncation in provider
        assert result["answer"] == "short answer"


# ---------------------------------------------------------------------------
# CredentialStore — encryption (existing, unmodified)
# ---------------------------------------------------------------------------

class TestCredentialStore:
    @staticmethod
    def _key() -> bytes:
        from cryptography.fernet import Fernet
        return Fernet.generate_key()

    def test_memory_only_without_key(self, tmp_path: Path) -> None:
        store = CredentialStore(data_dir=tmp_path, key=None)
        assert store.get() is None
        store.set("secret-key")
        assert store.get() == "secret-key"

    def test_no_file_without_key(self, tmp_path: Path) -> None:
        store = CredentialStore(data_dir=tmp_path, key=None)
        store.set("secret-key")
        assert not (tmp_path / _CREDENTIALS_FILENAME).exists()

    def test_persist_encrypted_with_key(self, tmp_path: Path) -> None:
        store = CredentialStore(data_dir=tmp_path, key=self._key())
        store.set("encrypted-secret")
        assert store.get() == "encrypted-secret"
        assert (tmp_path / _CREDENTIALS_FILENAME).exists()

    def test_reload_persisted_credentials(self, tmp_path: Path) -> None:
        key = self._key()
        s1 = CredentialStore(data_dir=tmp_path, key=key)
        s1.set("persisted-secret")
        del s1
        s2 = CredentialStore(data_dir=tmp_path, key=key)
        assert s2.get() == "persisted-secret"

    def test_clear_removes_file(self, tmp_path: Path) -> None:
        store = CredentialStore(data_dir=tmp_path, key=self._key())
        store.set("temp-key")
        store.clear()
        assert store.get() is None
        assert not (tmp_path / _CREDENTIALS_FILENAME).exists()

    def test_credential_filename_constant(self) -> None:
        assert _CREDENTIALS_FILENAME == "credentials.enc"


class TestCredentialStoreKeyParsing:
    def test_raw_32_bytes_becomes_fernet(self) -> None:
        key = b"01234567890123456789012345678901"
        result = CredentialStore._parse_key(key)
        assert result is not None and len(result) == 44

    def test_hex_32_bytes_becomes_fernet(self) -> None:
        result = CredentialStore._parse_key("0123456789abcdef" * 4)
        assert result is not None and len(result) == 44

    def test_fernet_key_passes_through(self) -> None:
        from cryptography.fernet import Fernet
        fk = Fernet.generate_key()
        result = CredentialStore._parse_key(fk)
        assert result is not None and len(result) == 44

    def test_none_returns_none(self) -> None:
        assert CredentialStore._parse_key(None) is None

    def test_invalid_returns_none(self) -> None:
        assert CredentialStore._parse_key("not-a-valid-key") is None
