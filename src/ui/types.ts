/** Common TypeScript typings for MyNotes — Electron bridge, backend DTOs, component props. */

/* ═══════════════════════════════════════════════════════════════════════════
 * Electron bridge – window.mynotes
 * ═══════════════════════════════════════════════════════════════════════════ */

/** Returned by `window.mynotes.connection()`. */
export interface ConnectionInfo {
  url: string;     // e.g. "http://127.0.0.1:9123"
  token: string;   // Bearer token
}

/** One item from the native file picker (`selectImport()`). */
export interface ImportedFile {
  name: string;
  data: ArrayBuffer;
  /** MIME type hint, e.g. "application/zip". */
  kind?: string;
  /** For typst/folder imports: the entry-point file name. */
  entrypoint?: string;
}

/** Electron bridge interface — provided by the main process. */
export interface ElectronBridge {
  getMicrophoneVolume(): Promise<{ volume: number; muted: boolean }>;
  setMicrophoneVolume(volume: number): Promise<{ volume: number; muted: boolean }>;
  selectDirectories(): Promise<string[]>;
  saveTextFile(request: { name: string; text: string; format: "md" | "txt" | "typ" }): Promise<{ path: string; name: string } | null>;
  connection(): Promise<ConnectionInfo>;
  selectImport(): Promise<ImportedFile | null>;
  saveRecordingChunk(sessionId: string, index: number, data: ArrayBuffer): Promise<void>;
  finishRecording(sessionId: string): Promise<{ name: string; data: ArrayBuffer }>;
}
declare global {
  interface Window { mynotes: ElectronBridge; }
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Blob URL cleanup management
 * ═══════════════════════════════════════════════════════════════════════════ */

/** Minimal registry so callers can revoke Blob URLs in tests. */
export interface BlobUrlRegistry {
  /** Track a Blob URL for later revocation. */
  add(url: string): void;
  /** Revoke every tracked URL and clear the set. */
  revokeAll(): void;
}

/** Singleton registry — exported so tests can revoke before assertions. */
export function createBlobUrlRegistry(): BlobUrlRegistry {
  const urls = new Set<string>();
  return {
    add(url: string): void {
      urls.add(url);
    },
    revokeAll(): void {
      urls.forEach((u) => URL.revokeObjectURL(u));
      urls.clear();
    },
  };
}

export const blobUrlRegistry: BlobUrlRegistry = createBlobUrlRegistry();

/* ═══════════════════════════════════════════════════════════════════════════
 * Backend DTOs — document metadata (PATCH /documents/{id})
 * ═══════════════════════════════════════════════════════════════════════════ */

export interface DocumentMeta {
  title: string;
  course_id: string;
  chapter: string;
  class_date: string;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Backend DTOs — courses
 * ═══════════════════════════════════════════════════════════════════════════ */

export interface Course {
  id: string;
  name: string;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Backend DTOs — documents, versions, blocks
 * ═══════════════════════════════════════════════════════════════════════════ */

export interface DocumentSummary {
  id: string;
  title: string;
  kind: string;
  course_id?: string;
  chapter?: string;
  class_date?: string;
  created_at: number;
  current_version_id?: string;
}

export interface AssetInfo {
  path: string;
  title?: string;
}

export interface Version {
  id: string;
  document_id: string;
  original_path?: string;
  created_at: number;
  blocks?: Array<{ id: string; text: string; locator?: Record<string, unknown>; origin?: string }>;
  assets?: AssetInfo[];
  warnings?: string[];
}

export interface Block {
  id: string;
  text: string;
  locator?: Record<string, unknown>;
  origin?: string;
  version_id: string;
  document_id: string;
  title: string;
  rank?: number;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Backend DTOs — jobs
 * ═══════════════════════════════════════════════════════════════════════════ */

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export interface Job {
  id: string;
  kind: string;
  payload?: Record<string, unknown>;
  status: JobStatus;
  progress: number;
  worker_id?: string;
  error?: string;
  result?: Record<string, unknown>;
  attempts: number;
  created_at: number;
  updated_at: number;
}

export interface JobBrief {
  id: string;
  status: JobStatus;
  kind: string;
  progress: number;
  error?: string;
  result?: Record<string, unknown>;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Backend DTOs — chat
 * ═══════════════════════════════════════════════════════════════════════════ */

export interface Citation {
    source_id: string;
    excerpt: string;
    version_id?: string;
    document_id?: string;
    locator?: Record<string, unknown>;
    title?: string;
}

export interface ChatMessage {
  id: string;
  question: string;
  answer: string;
  citations: Citation[];
  mode: string;
  warnings: string[];
  created_at: number;
}

export interface SuggestionResult {
  title?: string;
  course?: string;
  chapter?: string;
  class_date?: string;
  mode?: string;
  warnings: string[];
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Backend DTOs — slides / decks
 * ═══════════════════════════════════════════════════════════════════════════ */

export interface Deck {
  id: string;
  title: string;
  markdown: string;
  source_ids: string[];
  revision: number;
  created_at: number;
  updated_at: number;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Backend DTOs — search / material
 * ═══════════════════════════════════════════════════════════════════════════ */

export interface MaterialOut {
  mode: string;
  blocks: Block[];
  warnings: string[];
  estimated_tokens: number;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Backend DTOs — settings
 * ═══════════════════════════════════════════════════════════════════════════ */

export interface SettingsOut {
  agent_directories?: string[];
  chat_harness?: boolean;
  agent_available?: boolean;
  credential_storage?: "encrypted" | "session";
  credential_error?: string | null;
  realtime_model?: string;
  verified_realtime?: boolean;
  has_key?: boolean;
  base_url?: string;
  chat_model?: string;
  fast_model?: string;
  global_chat_model?: string;
  audio_summary_model?: string;
  document_summary_model?: string;
  timeout?: number;
  transcription_model?: string;
  vision_model?: string;
  chat_path?: string;
  transcription_path?: string;
  vision_path?: string;
  context_budget?: number;
  verified_chat?: boolean;
  verified_transcription?: boolean;
  verified_vision?: boolean;
  chat_endpoint_verified?: boolean;
  transcription_endpoint_verified?: boolean;
  vision_endpoint_verified?: boolean;
}

/* ═══════════════════════════════════════════════════════════════════════════
 * Component props — shared across tabs
 * ═══════════════════════════════════════════════════════════════════════════ */

export interface TabProps {
  /** Document IDs currently selected by the user. */
  selectedIds: string[];
  /** Callback when a background job is created or updates. */
  onJob: (job: JobBrief) => void;
}

export interface SourceCallback {
  (source: Block | Citation): void;
}
