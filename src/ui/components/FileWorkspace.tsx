import { useEffect, useRef, useState } from "react";
import { api, request } from "../api";

type Entry = { name: string; path: string; directory: boolean; editable: boolean; size: number };
type Page = { entries: Entry[]; next_offset: number | null };
type Draft = { path: string; text: string; sha256: string | null };
const KEY = "mynotes-laptop-file-drafts";
function recover(): Record<string, Draft> {
  try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch { return {}; }
}

export default function FileWorkspace() {
  const [pages, setPages] = useState<Record<string, Page>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({ "": true });
  const [directory, setDirectory] = useState("");
  const [file, setFile] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [drafts, setDrafts] = useState(recover);
  const [filename, setFilename] = useState("Untitled.md");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const requestId = useRef(0);

  const load = async (path: string, offset = 0) => {
    try {
      const result = await api<Page>(`/workspace/entries?path=${encodeURIComponent(path)}&offset=${offset}`);
      setPages(previous => ({ ...previous, [path]: { ...result, entries: offset ? [...(previous[path]?.entries || []), ...result.entries] : result.entries } }));
    } catch (e) { setError(e instanceof Error ? e.message : "Cannot read folder"); }
  };
  useEffect(() => { void load(""); }, []);
  const keepDraft = (draft: Draft) => {
    setDrafts(previous => {
      const next = { ...previous, [draft.path]: draft };
      try { localStorage.setItem(KEY, JSON.stringify(next)); }
      catch { setError("Recovery storage is full or unavailable. Save this draft to your file now."); }
      return next;
    });
  };
  const edit = (text: string) => {
    if (!file) return;
    ++requestId.current;
    const next = { ...file, text }; setFile(next); setDirty(true); setMessage(""); keepDraft(next);
  };
  const open = async (entry: Entry) => {
    const id = ++requestId.current;
    setError(""); setMessage("");
    if (entry.directory) {
      setDirectory(entry.path);
      setExpanded(previous => ({ ...previous, [entry.path]: !previous[entry.path] }));
      if (!expanded[entry.path] && !pages[entry.path]) await load(entry.path); return;
    }
    setDirectory(entry.path.slice(0, entry.path.lastIndexOf("/")));
    if (drafts[entry.path]) { setFile(drafts[entry.path]); setDirty(true); return; }
    if (!entry.editable) {
      setFile(null); setDirty(false);
      setMessage(`${entry.path} · ${(entry.size / 1024).toFixed(1)} KiB · read-only binary file`);
      return;
    }
    try {
      const result = await api<Draft>(`/workspace/text?path=${encodeURIComponent(entry.path)}`);
      if (id === requestId.current) { setFile(result); setDirty(false); }
    } catch (e) { setError(e instanceof Error ? e.message : "Cannot open file"); }
  };
  const save = async () => {
    if (!file || busy) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const saved = await api<{ path: string; sha256: string }>("/workspace/text", { method: "PUT", body: JSON.stringify({ path: file.path, text: file.text, expected_sha256: file.sha256 }) });
      setFile({ ...file, ...saved }); setDirty(false); setMessage(`Saved to ${saved.path}`);
      const next = { ...drafts }; delete next[file.path];
      setDrafts(next);
      try { localStorage.setItem(KEY, JSON.stringify(next)); }
      catch { setError("The original file was saved, but its recovery draft could not be cleared."); }
      if (directory) await load(directory);
    } catch (e) { setError(e instanceof Error ? e.message : "Save failed; draft retained"); }
    finally { setBusy(false); }
  };
  const tree = (path: string, depth = 0): React.ReactNode => <ul className="file-tree" aria-label={depth ? undefined : "Allowed folders"}>
    {(pages[path]?.entries || []).map(entry => <li className="file-tree-item" key={entry.path}>
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
      <h3>Files on laptop</h3><p className="field-hint">Folders selected in Settings. Files stay in their original location.</p>
      <button onClick={() => { setError(""); void load(""); for (const path of Object.keys(expanded)) if (path && expanded[path]) void load(path); }}>Refresh folders</button>
      <nav className="file-browser-tree" aria-label="Laptop files">{tree("")}</nav>
      {pages[""]?.entries.length === 0 && <p>Select allowed folders in Settings to browse your files.</p>}
      {Object.keys(drafts).length > 0 && <><h4>Recovery drafts</h4>{Object.values(drafts).map(draft => <button key={draft.path} title={draft.path} disabled={busy} onClick={() => { ++requestId.current; setFile(draft); setDirty(true); setMessage(""); setError(""); }}>{draft.path.split("/").pop()}</button>)}</>}
    </aside>
    <article className="file-editor">
      <h2>{file ? file.path.split("/").pop() : "Original laptop files"}</h2>
      <div className="button-row"><input aria-label="New file name" value={filename} onChange={e => setFilename(e.target.value)} disabled={busy} />
        <button disabled={!directory || busy} onClick={() => {
          if (!filename.trim() || /[\/\\]/.test(filename) || filename === "." || filename === "..") { setError("Choose a filename without folder separators"); return; }
          ++requestId.current; const next = { path: directory + "/" + filename, text: "", sha256: null };
          setFile(next); setDirty(true); keepDraft(next); setError(""); setMessage("");
        }}>New file in selected folder</button></div>
      {directory && <small className="muted">Selected folder: {directory}</small>}
      {file && <><code className="file-path" title={file.path}>{file.path}</code>
        <textarea aria-label="Laptop file content" value={file.text} onChange={e => edit(e.target.value)} disabled={busy} style={{ flex: 1, minHeight: 250 }} />
        <p role="status">{dirty ? "Recovery draft only — changes are not saved to the original file." : message || "Original file loaded from disk."}</p>
        <button className="primary-button" disabled={busy || !dirty} onClick={save}>{busy ? "Saving to file…" : "Save to original file"}</button>
      </>}
      {!file && message && <p role="status">{message}</p>}
      {!file && !message && <div className="file-empty"><span className="empty-symbol">▤</span><h3>Choose a file</h3><p>Expand a folder on the left to browse its files. Open a text file to edit it here, or select a folder to create a new note.</p></div>}
      {error && <div role="alert" className="error-banner">{error}</div>}
    </article>
  </section>;
}
