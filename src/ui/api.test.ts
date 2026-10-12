/**
 * Tests for src/ui/api.ts
 *
 * Three checks:
 *  1. api() builds the correct URL from connection.url and attaches the
 *     Authorization Bearer header.
 *  2. api() with FormData does NOT set Content-Type (browser sets boundary).
 *  3. A rejected connection() call resets the promise cache so future calls retry.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetConnectionCache, api } from "./api";

/* ── mocks ─────────────────────────────────────────────────────────────── */

declare global {
  // eslint-disable-next-line no-var
  var __fetchMock: ReturnType<typeof vi.fn>;
}

beforeEach(() => {
  _resetConnectionCache();

  (window as any).mynotes = {
    connection: () =>
      Promise.resolve({
        url: "http://127.0.0.1:9123",
        token: "test-token",
      }),
  };

  const mock = vi.fn();
  globalThis.fetch = mock;
  globalThis.__fetchMock = mock;
});

afterEach(() => {
  delete (globalThis as any).mynotes;
  delete (globalThis as any).__fetchMock;
});

/* ── helpers ───────────────────────────────────────────────────────────── */

function lastCall(): [string, RequestInit | undefined] {
  const calls = (globalThis.__fetchMock as ReturnType<typeof vi.fn>).mock.calls;
  const last = calls[calls.length - 1];
  return [last[0] as string, last[1]];
}

/* ── tests ─────────────────────────────────────────────────────────────── */

describe("api()", () => {
  it("normalizes 202 job acknowledgements for polling and sends JSON content type", async () => {
    globalThis.__fetchMock.mockResolvedValue(new Response(JSON.stringify({ job_id: "j1", status: "queued" }), {
      status: 202, headers: { "content-type": "application/json" },
    }));
    const job = await api<{ id: string; job_id: string }>("/notes", { method: "POST", body: JSON.stringify({ title: "Note" }) });
    expect(job.id).toBe("j1");
    expect(job.job_id).toBe("j1");
    expect(lastCall()[1]?.headers).toMatchObject({ "Content-Type": "application/json" });
  });
  it("builds URL from connection.url and attaches Authorization header", async () => {
    (globalThis.__fetchMock as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ id: "1", name: "test" }),
      text: () => Promise.resolve('{"id":"1","name":"test"}'),
    });

    await api<{ id: string; name: string }>("/documents");

    const [url, init] = lastCall();

    // connection.url is the root — NOT /api/v1.
    expect(url).toBe("http://127.0.0.1:9123/documents");
    expect(init?.headers).toMatchObject({
      Authorization: "Bearer test-token",
    });
  });

  it("omits Content-Type when body is FormData", async () => {
    (globalThis.__fetchMock as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ ok: true }),
      text: () => Promise.resolve('{"ok":true}'),
    });

    const fd = new FormData();
    fd.append("file", new Blob(["data"]), "file.txt");

    await api("/upload", { method: "POST", body: fd });

    const [url, init] = lastCall();

    expect(url).toBe("http://127.0.0.1:9123/upload");

    // Content-Type must NOT be set — browser sets the multipart boundary.
    if (init?.headers && typeof init.headers === "object" && !Array.isArray(init.headers)) {
      expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
    }

    // Authorization must still be present.
    if (init?.headers && typeof init.headers === "object" && !Array.isArray(init.headers)) {
      expect((init.headers as Record<string, string>)["Authorization"]).toBe("Bearer test-token");
    }
  });

  it("resets connect promise on connection() failure so future calls retry", async () => {
    const mock = globalThis.__fetchMock as ReturnType<typeof vi.fn>;

    // Simulate: first call bridge.connection() fails (e.g. network down).
    // Subsequent calls to api() should retry getConnection().
    (window as any).mynotes.connection = vi.fn()
      .mockRejectedValueOnce(new Error("bridge unavailable"))
      .mockResolvedValue({ url: "http://127.0.0.1:9123", token: "recovered" });

    mock.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ id: "1" }),
      text: () => Promise.resolve('{"id":"1"}'),
    });

    // First attempt fails → getConnection() promise rejects → _connectPromise reset.
    await api("/documents").catch(() => {});

    // Second attempt: getConnection() is called again (promise was reset).
    await api<{ id: string }>("/documents");

    // connection() was called twice (first rejected, second succeeded).
    expect((window as any).mynotes.connection).toHaveBeenCalledTimes(2);
  });
});
