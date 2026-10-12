/**
 * Shared HTTP helpers for the React frontend.
 *
 * Auth flow
 * ─────────
 * On first use `api()` calls `window.mynotes.connection()` to obtain
 * `{ url, token }`.  The token is cached in memory (cleared on 401).
 *
 * Multipart
 * ─────────
 * When `init.body` is a `FormData` instance the `Content-Type` header
 * is **not** set — the browser adds the correct boundary.
 */

import type { ConnectionInfo } from "./types";
import { blobUrlRegistry } from "./types";
export { blobUrlRegistry } from "./types";

/* ── connection cache ─────────────────────────────────────────────────── */

let _connection: ConnectionInfo | null = null;
let _connectPromise: Promise<ConnectionInfo> | null = null;

export async function getConnection(): Promise<ConnectionInfo> {
  if (_connection) return _connection;
  if (_connectPromise) return _connectPromise;
  _connectPromise = (async () => {
    const bridge = (window as any).mynotes as { connection?(): Promise<ConnectionInfo> } | undefined;
    if (!bridge?.connection) throw new Error("Electron bridge 'connection()' not available");
    const info = await bridge.connection();
    _connection = info;
    return info;
  })();
  // Reset on rejection so future calls retry (connection is recoverable).
  _connectPromise.catch(() => { _connectPromise = null; });
  return _connectPromise;
}

function clearConnection(): void {
  _connection = null;
  _connectPromise = null;
}

/* ── base URL from connection ─────────────────────────────────────────── */

function base(): string {
  return (_connection?.url ?? "");
}

/* ── api<T> — JSON helpers ────────────────────────────────────────────── */

export async function api<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const conn = await getConnection();
  const url = conn.url + path;
  const headers: Record<string, string> = {};

  if (!(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  // Build from init.headers then set Authorization LAST so nothing overwrites it.
  const merged: Record<string, string> = { ...headers };
  if (init.headers instanceof Headers) {
    init.headers.forEach((v, k) => { merged[k] = v; });
  } else if (typeof (init.headers as any)?.entries === "function") {
    for (const [k, v] of (init.headers as any).entries()) {
      merged[k] = v as string;
    }
  } else if (init.headers && typeof (init.headers as object) === "object") {
    Object.assign(merged, init.headers as Record<string, string>);
  }
  merged["Authorization"] = `Bearer ${conn.token}`;

  const res = await fetch(url, {
    ...init,
    headers: merged,
  });

  // clear cached token on 401 so next call re-connects
  if (res.status === 401) {
    clearConnection();
  }

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  if (res.status === 204) return undefined as unknown as T;
  if (res.headers.get("content-type")?.includes("application/json")) {
    const data = await res.json();
    // Keep the backend's 202 job_id contract; expose the ID used by UI polling.
    if (res.status === 202 && data.job_id) return { ...data, id: data.job_id } as T;
    return data as T;
  }
  return await res.text() as T;
}

/* ── request — raw Response for text / blobs ──────────────────────────── */

export async function request(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const conn = await getConnection();
  const url = conn.url + path;
  const headers: Record<string, string> = {};

  if (!(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const merged: Record<string, string> = { ...headers };
  if (init.headers instanceof Headers) {
    init.headers.forEach((v, k) => { merged[k] = v; });
  } else if (typeof (init.headers as any)?.entries === "function") {
    for (const [k, v] of (init.headers as any).entries()) {
      merged[k] = v as string;
    }
  } else if (init.headers && typeof (init.headers as object) === "object") {
    Object.assign(merged, init.headers as Record<string, string>);
  }
  merged["Authorization"] = `Bearer ${conn.token}`;

  const res = await fetch(url, {
    ...init,
    headers: merged,
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res;
}

/* ── fileUrl — build a URL to a managed file ──────────────────────────── */

function normalizePath(path: string): string {
  return path
    .replace(/^\//, "")                          // strip leading /
    .split("/")
    .map((s) => encodeURIComponent(s))           // encode each segment
    .join("/");
}

export function fileUrl(path: string): string {
  return `${base()}/files/${normalizePath(path)}`;
}

/* ── downloadFile — fetch a file as Blob ──────────────────────────────── */

export async function downloadFile(path: string): Promise<Blob> {
  const res = await request(`/files/${normalizePath(path)}`);
  return res.blob();
}

/* ── secureFileUrl — Blob URL for <audio>/<iframe>/<img> ───────────────── */

export async function secureFileUrl(path: string): Promise<string> {
  const blob = await downloadFile(path);
  const url = URL.createObjectURL(blob);
  blobUrlRegistry.add(url);
  return url;
}

/* ── terminal job statuses ─────────────────────────────────────────────── */

export const TERMINAL = new Set(["succeeded", "failed"]);

/* ── cleanup helpers (useful in tests) ─────────────────────────────────── */

export function _resetConnectionCache(): void {
  blobUrlRegistry.revokeAll();
  clearConnection();
}
