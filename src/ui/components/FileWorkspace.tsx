import { useEffect, useRef, useState } from "react";
import { api, request } from "../api";
import MarkdownEditor from "./MarkdownEditor";

type Entry = { name: string; path: string; directory: boolean; editable: boolean; size: number };
type Page = { entries: Entry[]; next_offset: number | null };
type Draft = { path: string; text: string; sha256: string | null };
const KEY = "mynotes-laptop-file-drafts";
function recover(): Record<string, Draft> {
  try {
    const data = JSON.parse(localStorage.getItem(KEY) || "{}");
    if (!data || typeof data !== "object" || Array.isArray(data)) return {};
    return Object.fromEntries(Object.entries(data).filter(([path, value]: [string, any]) => path.startsWith("/") && value && value.path === path && typeof value.text === "string" && (value.sha256 === null || typeof value.sha256 === "string"))) as Record<string, Draft>;
  } catch { return {}; }
}
const parentOf = (path: string) => path.slice(0, path.lastIndexOf("/")) || "/";

export default function FileWorkspace() {
  const [pages, setPages] = useState<Record<string, Page>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({ "": true });
  const [directory, setDirectory] = useState("");
  const [file, setFile] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [drafts, setDrafts] = useState(recover);
  const [filename, setFilename] = useState("Untitled.md");
  const [foldername, setFoldername] = useState("New folder");
  const [autosave, setAutosave] = useState(() => { try { return localStorage.getItem("mynotes-file-autosave") === "true"; } catch { return false; } });
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [disk, setDisk] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const requestId = useRef(0);
  const creationPanel = useRef<HTMLDetailsElement>(null);
  const alive = useRef(true);
  const writing = useRef(false);
  const fileRef = useRef(file);
  const draftsRef = useRef(drafts);
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef(new Map<string, Promise<void>>());
  const selectFile = (next: Draft | null) => { fileRef.current = next; setFile(next); };
  const flushDrafts = () => {
    if (draftTimer.current) clearTimeout(draftTimer.current);
    draftTimer.current = null;
    try { localStorage.setItem(KEY, JSON.stringify(draftsRef.current)); }
    catch { if (alive.current) setError("Recovery storage is full or unavailable. Save this note to its original file now."); }
  };
  const updateDrafts = (next: Record<string, Draft>, immediately = false) => {
    draftsRef.current = next; setDrafts(next);
    if (immediately) flushDrafts();
    // ponytail: recovery writes are throttled to 200 ms; flush on navigation/pagehide limits the crash window.
    else if (!draftTimer.current) draftTimer.current = setTimeout(flushDrafts, 200);
  };

  const load = async (path: string, offset = 0, fresh = false): Promise<void> => {
    const key = `${path}:${offset}`, existing = pending.current.get(key);
    if (existing) { await existing; if (fresh) await load(path, offset); return; }
    const task = (async () => {
      try {
        const result = await api<Page>(`/workspace/entries?path=${encodeURIComponent(path)}&offset=${offset}`);
        if (!alive.current) return;
        setPages(previous => ({ ...previous, [path]: { ...result, entries: offset ? [...(previous[path]?.entries || []), ...result.entries] : result.entries } }));
        if (!path) setDirectory(previous => previous || result.entries[0]?.path || "");
      } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "Cannot read folder"); }
    })();
    pending.current.set(key, task);
    try { await task; } finally { pending.current.delete(key); }
  };
  useEffect(() => {
    alive.current = true; void load("");
    window.addEventListener("pagehide", flushDrafts);
    return () => { alive.current = false; window.removeEventListener("pagehide", flushDrafts); flushDrafts(); };
  }, []);
  useEffect(() => { try { localStorage.setItem("mynotes-file-autosave", String(autosave)); } catch {} }, [autosave]);
  const keepDraft = (draft: Draft) => {
    updateDrafts({ ...draftsRef.current, [draft.path]: draft });
  };
  const edit = (text: string) => {
    if (!fileRef.current) return;
    ++requestId.current;
    const next = { ...fileRef.current, text }; selectFile(next); setDirty(true); setMessage(""); keepDraft(next);
  };
  const open = async (entry: Entry) => {
    const id = ++requestId.current;
    flushDrafts();
    setError(""); setMessage("");
    setConflict(false); setDisk(null);
    if (entry.directory) {
      setDirectory(entry.path);
      setExpanded(previous => ({ ...previous, [entry.path]: !previous[entry.path] }));
      if (!expanded[entry.path] && !pages[entry.path]) await load(entry.path); return;
    }
    setDirectory(parentOf(entry.path));
    if (draftsRef.current[entry.path]) { selectFile(draftsRef.current[entry.path]); setDirty(true); return; }
    selectFile(null); setDirty(false);
    if (!entry.editable) {
      setMessage(`${entry.path} · ${(entry.size / 1024).toFixed(1)} KiB · read-only binary file`);
      return;
    }
    try {
      setMessage("Opening file…");
      const result = await api<Draft>(`/workspace/text?path=${encodeURIComponent(entry.path)}`);
      if (alive.current && id === requestId.current) { selectFile(result); setDirty(false); setMessage(""); }
    } catch (e) { if (alive.current && id === requestId.current) { setMessage(""); setError(e instanceof Error ? e.message : "Cannot open file"); } }
  };
  const save = async () => {
    const snapshot = fileRef.current;
    if (!snapshot || writing.current || !dirty) return;
    writing.current = true; flushDrafts();
    setBusy(true); setError(""); setMessage("");
    try {
      const saved = await api<{ path: string; sha256: string }>("/workspace/text", { method: "PUT", body: JSON.stringify({ path: snapshot.path, text: snapshot.text, expected_sha256: snapshot.sha256 }) });
      if (!alive.current) {
        const latest = recover(), draft = latest[snapshot.path];
        if (draft?.text === snapshot.text) delete latest[snapshot.path];
        else if (draft) latest[snapshot.path] = { ...draft, sha256: saved.sha256 };
        try { localStorage.setItem(KEY, JSON.stringify(latest)); } catch {}
        return;
      }
      const current = fileRef.current;
      if (!current || current.path !== snapshot.path) return;
      const changed = current.text !== snapshot.text, next = { ...current, sha256: saved.sha256 };
      selectFile(next); setDirty(changed);
      const remaining = { ...draftsRef.current };
      if (changed) remaining[next.path] = next; else delete remaining[next.path];
      updateDrafts(remaining, true);
      setMessage(changed ? "Earlier edits saved; your latest edits are still pending." : `Saved to ${saved.path}`);
      // Text edits do not change the tree: do not rescan a large folder on every save.
    } catch (e) {
      if (alive.current) { setAutosave(false); flushDrafts(); setConflict(e instanceof Error && e.message.startsWith("409")); setError((e instanceof Error ? e.message : "Save failed") + " — draft retained; autosave stopped."); }
    } finally { writing.current = false; if (alive.current) setBusy(false); }
  };
  useEffect(() => {
    if (!autosave || !dirty || !file || busy) return;
    const timer = setTimeout(() => { void save(); }, 1000);
    return () => clearTimeout(timer);
  }, [autosave, dirty, file, busy]);
  const reviewDisk = async () => {
    if (!fileRef.current || busy) return;
    setBusy(true);
    try { setDisk(await api<Draft>(`/workspace/text?path=${encodeURIComponent(fileRef.current.path)}`)); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not read original"); }
    finally { setBusy(false); }
  };

  const add = async (files: boolean) => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const selected = files ? await window.mynotes.selectWorkspaceFiles() : await window.mynotes.selectDirectories();
      if (!selected.length) return;
      const folders = files ? selected.map(parentOf) : selected;
      const settings = await api<{ agent_directories?: string[] }>("/settings");
      await api("/settings", { method: "PUT", body: JSON.stringify({ agent_directories: [...new Set([...(settings.agent_directories || []), ...folders])] }) });
      if (!alive.current) return;
      await load("", 0, true); setDirectory(folders[0]);
      setExpanded(previous => ({ ...previous, [folders[0]]: true })); await load(folders[0], 0, true);
      if (files) await open({ name: selected[0].split("/").pop()!, path: selected[0], directory: false, editable: true, size: 0 });
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "Could not add files or folders"); }
    finally { if (alive.current) setBusy(false); }
  };
  const create = async (folder: boolean) => {
    if (!directory || busy) return;
    let name = (folder ? foldername : filename).trim();
    if (!name || /[\/\\]/.test(name) || name === "." || name === "..") { setError("Choose a name without folder separators"); return; }
    if (!folder && !/\.[^.]+$/.test(name)) name += ".md";
    const parent = directory, path = parent.replace(/\/$/, "") + "/" + name;
    setBusy(true); setError(""); setConflict(false); setDisk(null); flushDrafts();
    try {
      if (folder) {
        const created = await api<{ path: string }>("/workspace/directories", { method: "POST", body: JSON.stringify({ path }) });
        setDirectory(created.path); setExpanded(previous => ({ ...previous, [parent]: true, [created.path]: true }));
        await load(created.path);
      } else {
        const created = await api<{ path: string; sha256: string }>("/workspace/text", { method: "PUT", body: JSON.stringify({ path, text: "", expected_sha256: null }) });
        ++requestId.current; selectFile({ ...created, text: "" }); setDirty(false); setMessage(`Created ${created.path}`);
        if (creationPanel.current) creationPanel.current.open = false;
        setExpanded(previous => ({ ...previous, [parent]: true }));
      }
      await load(parent, 0, true);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not create item"); }
    finally { setBusy(false); }
  };
  const tree = (path: string, depth = 0): React.ReactNode => <ul className="file-tree" aria-label={depth ? undefined : "Allowed folders"}>
    {(pages[path]?.entries || []).map(entry => <li className="file-tree-item" key={`${entry.path}:${entry.name}`}>
      <div className="file-tree-row">
      <button type="button" title={entry.path} aria-expanded={entry.directory ? !!expanded[entry.path] : undefined}
        className={`file-tree-entry ${file?.path === entry.path || directory === entry.path ? "selected" : ""}`} disabled={busy} onClick={() => open(entry)}>
        <span className="file-tree-icon" aria-hidden="true">{entry.directory ? expanded[entry.path] ? "▾" : "▸" : "▤"}</span><span>{entry.name}</span>
      </button>
      {!entry.directory && !entry.editable && <button type="button" onClick={async () => {
        try {
          const response = await request(`/workspace/file?path=${encodeURIComponent(entry.path)}`);
          if (!response.ok) throw new Error("Cannot download file");
          const url = URL.createObjectURL(await response.blob());
          const a = document.createElement("a"); a.href = url; a.download = entry.name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (e) { setError(e instanceof Error ? e.message : "Download failed"); }
      }} aria-label={`Download ${entry.name}`} title="Download file" className="file-download">↓</button>}
      </div>
      {entry.directory && expanded[entry.path] && depth < 40 && tree(entry.path, depth + 1)}
    </li>)}
    {pages[path]?.next_offset != null && <li><button onClick={() => load(path, pages[path].next_offset!)}>Load more files</button></li>}
  </ul>;
  return <section className="file-workspace">
    <aside className="file-browser">
      <h3>Files & notes</h3>
      <div className="workspace-actions"><button disabled={busy} onClick={() => add(false)}>Add folder…</button><button disabled={busy} onClick={() => add(true)}>Open files…</button>
        <button disabled={busy} onClick={() => { setError(""); void load("", 0, true); for (const path of Object.keys(expanded)) if (path && expanded[path]) void load(path, 0, true); }}>Refresh folders</button></div>
      <p className="field-hint">Original files stay on disk. Opening a file also allows its parent folder in this workspace and the assistant.</p>
      <nav className="file-browser-tree" aria-label="Laptop files">{tree("")}</nav>
      {pages[""]?.entries.length === 0 && <p>Click Add folder… or Open files… to get started.</p>}
      {Object.keys(drafts).length > 0 && <><h4>Unsaved recovery drafts</h4>{Object.values(drafts).map(draft => <button key={draft.path} title={draft.path} disabled={busy} onClick={() => { ++requestId.current; selectFile(draft); setDirectory(parentOf(draft.path)); setDirty(true); setMessage(""); setError(""); }}>{draft.path.split("/").pop()}</button>)}</>}
    </aside>
    <article className="file-editor" onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); void save(); } }}>
      <h2>{file ? file.path.split("/").pop() : "Original laptop files"}</h2>
      <details className="workspace-new" ref={creationPanel} open={!file}><summary>New note or folder</summary><div className="workspace-create"><label>Note or file name<input aria-label="New file name" value={filename} onChange={e => setFilename(e.target.value)} disabled={busy} /></label><button disabled={!directory || busy} onClick={() => create(false)}>Create note</button>
        <label>Subfolder name<input aria-label="New folder name" value={foldername} onChange={e => setFoldername(e.target.value)} disabled={busy} /></label><button disabled={!directory || busy} onClick={() => create(true)}>Create folder</button></div></details>
      {directory && <small className="muted">Selected folder: {directory}</small>}
      {file && <><code className="file-path" title={file.path}>{file.path}</code>
        {/\.(md|markdown)$/i.test(file.path) ? <MarkdownEditor value={file.text} onChange={edit} label="Laptop file content" /> : <textarea aria-label="Laptop file content" value={file.text} onChange={e => edit(e.target.value)} style={{ flex: 1, minHeight: 250 }} />}
        <p role="status">{busy ? writing.current ? "Saving to original file… You can keep typing." : "Working…" : dirty ? autosave ? "Unsaved edits — autosave in one second." : "Recovery draft only — changes are not saved to the original file." : message || "Original file loaded from disk."}</p>
        <div className="button-row file-actions"><button className="primary-button" disabled={busy || !dirty} onClick={save}>{busy ? "Saving to file…" : "Save to original file"}</button><label className="check-label"><input type="checkbox" checked={autosave} onChange={e => setAutosave(e.target.checked)} />Autosave to original file</label><small className="muted">Ctrl+S to save</small></div>
      </>}
      {!file && message && <p role="status">{message}</p>}
      {!file && !message && <div className="file-empty"><span className="empty-symbol">▤</span><h3>Choose a file</h3><p>Expand a folder on the left to browse its files. Open a text file to edit it here, or select a folder to create a new note.</p></div>}
      {error && <div role="alert" className="error-banner">{error}</div>}
      {conflict && file && <div className="file-conflict"><button disabled={busy} onClick={reviewDisk}>Review disk version</button>
        {disk && <><label>Current original file<textarea aria-label="Current disk version" value={disk.text} readOnly /></label><div className="button-row">
          <button disabled={busy} onClick={() => { const next = { ...fileRef.current!, sha256: disk.sha256 }; selectFile(next); keepDraft(next); setConflict(false); setDisk(null); void save(); }}>Save my draft over reviewed version</button>
          <button disabled={busy} onClick={() => { selectFile(disk); setDirty(false); const next = { ...draftsRef.current }; delete next[file.path]; updateDrafts(next, true); setConflict(false); setDisk(null); setError(""); setMessage("Original disk version loaded."); }}>Use disk version</button>
        </div></>}
      </div>}
    </article>
  </section>;
}
