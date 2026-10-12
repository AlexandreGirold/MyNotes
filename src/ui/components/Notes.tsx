import { useEffect, useState } from "react";
import type { Block, Course, JobBrief, TabProps, Version } from "../types";
import FileWorkspace from "./FileWorkspace";

interface NotesProps extends TabProps {
  jobs: Record<string, JobBrief>;
  noteFormat: "markdown" | "typst";
  noteTitle: string;
  noteContent: string;
  noteCourse: string;
  setNoteFormat: (f: "markdown" | "typst") => void;
  setNoteTitle: (t: string) => void;
  setNoteContent: (c: string) => void;
  setNoteCourse: (c: string) => void;
  selectedDoc: any;
  version: Version | null;
  saveNote: (title: string, content: string, format: string, courseId?: string) => Promise<any>;
  updateNote: (noteId: string, title: string, content: string, format: string, courseId?: string) => Promise<any>;
  saveMetadata: (docId: string, title: string, courseId: string, chapter: string, classDate: string) => Promise<any>;
  loadDocumentDetails: (id: string) => Promise<void>;
  toggleSelect: (id: string) => void;
  documents: any[];
  searchQuery: string;
  setSearchQuery: (q: string) => void;
  doSearch: (q: string, courseId?: string) => Promise<void>;
  searchResults: Block[];
  searchLoading: boolean;
  courses: Course[];
  selectedCourseId: string;
  onCourseFilterChange: (id: string) => void;
  onSource: (source: Block) => void;
  downloadFile: (path: string) => Promise<void>;
  deleteDocument?: (id: string) => Promise<void>;
}

function SafeText({ children }: { children: string }) {
  const lines = children.split("\n").filter((l) => l.trim());
  if (!lines.length) return <em>No content</em>;
  return (
    <div>
      {lines.map((l, i) => (
        <div key={i} style={{ whiteSpace: "pre-wrap", margin: 0, lineHeight: 1.5 }}>{l}</div>
      ))}
    </div>
  );
}

const CONTENT_KINDS = ["markdown", "md", "typst", "typ"];

export default function Notes({
  selectedIds, jobs, noteFormat, noteTitle, noteContent, noteCourse,
  setNoteFormat, setNoteTitle, setNoteContent, setNoteCourse,
  saveMetadata, selectedDoc, version, saveNote, updateNote,
  loadDocumentDetails, toggleSelect,
  documents, searchQuery, setSearchQuery, doSearch, searchResults, searchLoading,
  courses, selectedCourseId, onCourseFilterChange, onSource, downloadFile, deleteDocument,
}: NotesProps) {
  const [error, setError] = useState("");
  const [showPreview, setShowPreview] = useState(false);
  const [previewMode, setPreviewMode] = useState<"raw" | "rendered">("rendered");
  const [showSearch, setShowSearch] = useState(false);
  const [chapter, setChapter] = useState("");
  const [classDate, setClassDate] = useState("");
  const [view, setView] = useState<"files" | "library">("files");

  const isContentKind = !selectedDoc || CONTENT_KINDS.includes(selectedDoc.kind);

  useEffect(() => {
    if (selectedIds.length === 1) loadDocumentDetails(selectedIds[0]);
  }, [selectedIds, loadDocumentDetails]);

  useEffect(() => {
    if (selectedDoc) {
      setNoteTitle(selectedDoc.title); setNoteCourse(selectedDoc.course_id || "");
      setChapter(selectedDoc.chapter || ""); setClassDate(selectedDoc.class_date || "");
    }
  }, [selectedDoc, setNoteTitle]);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!noteTitle.trim()) { setError("Title is required"); return; }
    try {
      if (selectedIds.length === 1 && selectedDoc?.current_version_id) {
        if (isContentKind) {
          await updateNote(selectedIds[0], noteTitle.trim(), noteContent, noteFormat, noteCourse.trim() || undefined);
        } else {
          await saveMetadata(selectedIds[0], noteTitle.trim(), noteCourse.trim(), chapter, classDate);
        }
      } else {
        await saveNote(noteTitle.trim(), isContentKind ? noteContent : "", noteFormat, noteCourse.trim() || undefined);
      }
    } catch (err) { setError(err instanceof Error ? err.message : "Save failed"); }
  };

  const S: Record<string, React.CSSProperties> = {
    wrap: { display: "flex", gap: 16, height: "100%" },
    sidebar: { width: 220, flexShrink: 0, borderRight: "1px solid #ddd", paddingRight: 12, display: "flex", flexDirection: "column", gap: 8, overflow: "auto" },
    docItem: { padding: "4px 8px", cursor: "pointer", borderRadius: 4, fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" },
    editor: { flex: 1, display: "flex", flexDirection: "column", gap: 12, overflow: "hidden" },
    preview: { flex: 1, border: "1px solid #ddd", borderRadius: 6, padding: 16, overflow: "auto", background: "#fafafa" },
    field: { padding: 8, borderRadius: 4, border: "1px solid #ccc", width: "100%", boxSizing: "border-box", fontFamily: "inherit" },
    textarea: { flex: 1, padding: 12, borderRadius: 4, border: "1px solid #ccc", width: "100%", boxSizing: "border-box", fontFamily: "monospace", fontSize: 13, resize: "none" },
    btn: { padding: "8px 16px", borderRadius: 4, border: "1px solid #ccc", background: "#f5f5f5", cursor: "pointer" },
    btnPrimary: { padding: "8px 16px", borderRadius: 4, border: "none", background: "#2563eb", color: "#fff", cursor: "pointer" },
    error: { color: "#dc2626", background: "#fef2f2", padding: "6px 12px", borderRadius: 4, fontSize: 13 },
    select: { padding: "8px 12px", borderRadius: 4, border: "1px solid #ccc", background: "#fff", cursor: "pointer" },
    searchInput: { padding: "6px 8px", borderRadius: 4, border: "1px solid #ccc", width: "100%", boxSizing: "border-box" },
  };

  const running = Object.values(jobs).find((j) => j.status === "queued" || j.status === "running");

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 12 }}>
      <div className="button-row"><button onClick={() => setView("files")}>Files on laptop</button><button onClick={() => setView("library")}>Recovery library & imports</button></div>
      {view === "files" && <FileWorkspace />}
      <div style={{ ...S.wrap, display: view === "library" ? "flex" : "none" }}>
      <div style={S.sidebar}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0, fontSize: 14 }}>Documents</h3>
          <button style={{ ...S.btn, fontSize: 12, padding: "2px 8px" }} onClick={() => setShowSearch((v) => !v)} aria-label="Toggle search">🔍</button>
        </div>
        <select aria-label="Filter course" value={selectedCourseId || ""} onChange={e => onCourseFilterChange(e.target.value)}>
          <option value="">All courses</option>{courses.map(course => <option key={course.id} value={course.id}>{course.name}</option>)}
        </select>
        {showSearch && (
          <div style={{ display: "flex", gap: 4 }}>
            <input style={S.searchInput} placeholder="Search..." value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && searchQuery.trim()) doSearch(searchQuery, selectedCourseId || undefined); }} aria-label="Search documents" />
            <button style={{ ...S.btn, fontSize: 12 }} onClick={() => searchQuery.trim() && doSearch(searchQuery, selectedCourseId || undefined)}>Go</button>
          </div>
        )}
        {searchLoading && <span style={{ fontSize: 12, color: "#888" }}>Searching...</span>}
        {showSearch && searchResults.length > 0 && (
          <div>
            <div style={{ fontSize: 11, fontWeight: 600, marginBottom: 4 }}>Results ({searchResults.length})</div>
            {searchResults.map((b) => (
              <div key={b.id} style={{ fontSize: 12, padding: "2px 4px", cursor: "pointer", borderRadius: 2 }}
                onClick={() => onSource(b)}>
                <div style={{ fontWeight: 500 }}>{b.title}</div>
                <div style={{ fontSize: 11, color: "#666", overflow: "hidden", textOverflow: "ellipsis" }}>{b.text.slice(0, 100)}...</div>
              </div>
            ))}
          </div>
        )}
        <ul className="library-document-list" style={{ listStyle: "none", padding: 0, margin: 0, flex: 1, overflow: "auto" }}>
          {documents.map((d) => (
            <li key={d.id} style={{ ...S.docItem, background: selectedIds.includes(d.id) ? "#dbeafe" : "transparent" }}
              onClick={() => toggleSelect(d.id)}><span>{d.title}</span>{deleteDocument && <button type="button" aria-label={`Delete ${d.title}`} title="Delete from library" onClick={async e => { e.stopPropagation(); try { await deleteDocument(d.id); } catch (failure) { setError(failure instanceof Error ? failure.message : "Delete failed"); } }}>×</button>}</li>
          ))}
        </ul>
      </div>
      <div style={S.editor}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <h2 style={{ margin: 0, fontSize: 16 }}>Notes</h2>
          <select value={noteFormat} onChange={(e) => setNoteFormat(e.target.value as "markdown" | "typst")}
            aria-label="Note format" style={S.select}>
            <option value="markdown">Markdown</option><option value="typst">Typst</option>
          </select>
          {selectedDoc && <span style={{ fontSize: 12, color: "#888", marginLeft: "auto" }} title={`Kind: ${selectedDoc.kind}`}>Doc: {selectedDoc.title}</span>}
        </div>
        {error && <div role="alert" aria-live="assertive" style={S.error}>{error}</div>}
        {Object.values(jobs).filter((j) => j.status !== "succeeded" && j.status !== "failed").map((j) => (
          <div key={j.id} style={{ fontSize: 12, color: "#555", background: "#f0f0f0", padding: "4px 8px", borderRadius: 4 }}>
            {j.status === "queued" && "Queued..."}{j.status === "running" && "Running..."}
            {j.progress != null && ` ${Math.round(j.progress * 100)}%`}
          </div>
        ))}
        <form onSubmit={handleSave} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", gap: 8 }}>
            <div style={{ flex: 1 }}>
              <label htmlFor="note-title" style={{ display: "block", fontSize: 12, marginBottom: 2 }}>Title *</label>
              <input id="note-title" type="text" value={noteTitle} onChange={(e) => setNoteTitle(e.target.value)}
                placeholder="Note title" required aria-required="true" style={S.field} />
            </div>
            <div style={{ flex: 1 }}>
              <label htmlFor="note-course" style={{ display: "block", fontSize: 12, marginBottom: 2 }}>Course</label>
              <select id="note-course" value={noteCourse} onChange={(e) => setNoteCourse(e.target.value)} style={S.field}>
                <option value="">No course</option>
                {courses.map(course => <option key={course.id} value={course.id}>{course.name}</option>)}
              </select>
            </div>
          </div>
          {selectedDoc && <fieldset><legend>Organization</legend>
            <label>Chapter <input value={chapter} onChange={e => setChapter(e.target.value)} /></label>
            <label>Class date <input type="date" value={classDate} onChange={e => setClassDate(e.target.value)} /></label>
            <button type="button" onClick={async () => {
              try { await saveMetadata(selectedDoc.id, noteTitle, noteCourse, chapter, classDate); }
              catch (e) { setError(e instanceof Error ? e.message : "Metadata update failed"); }
            }}>Save organization</button>
          </fieldset>}
          <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
            <label htmlFor="note-content" style={{ display: "block", fontSize: 12, marginBottom: 2 }}>
              Content ({noteFormat})
            </label>
            <textarea
              id="note-content"
              value={noteContent}
              onChange={(e) => setNoteContent(e.target.value)}
              placeholder={isContentKind ? `Write your ${noteFormat} note here...` : "Only metadata (title, course) can be edited for imported files"}
              style={S.textarea}
              aria-label={isContentKind ? "Note content" : "Note content (metadata only)"}
              disabled={!isContentKind}
            />
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button type="submit" style={S.btnPrimary} disabled={!!running}>
              {selectedDoc?.kind && !isContentKind ? "Save metadata" : "Save recovery copy"}
            </button>
            <button type="button" style={S.btn} onClick={() => setShowPreview((v) => !v)}>Preview</button>
            {isContentKind && <button type="button" onClick={async () => {
              try { await window.mynotes.saveTextFile({ name: noteTitle || "Note", text: noteContent, format: noteFormat === "typst" ? "typ" : "md" }); }
              catch (failure) { setError(failure instanceof Error ? failure.message : "Save as failed"); }
            }}>Save as…</button>}
            {selectedDoc && version && <button type="button" onClick={() => onSource({
              ...(version.blocks?.[0] || { id: version.id, text: "" }), version_id: version.id,
              document_id: selectedDoc.id, title: selectedDoc.title,
            } as Block)}>Open source</button>}
            {version?.assets?.map((a) => a.path.endsWith(".pdf")
              ? <button key={a.path} type="button" style={S.btn} onClick={() => downloadFile(a.path)}>{a.path.split("/").pop()}</button>
              : null)}
          </div>
        </form>
        {showPreview && (
          <div style={{ ...S.preview, marginTop: 8 }} role="region" aria-label="Preview">
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
              <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>Preview</h3>
              <div style={{ display: "flex", gap: 4 }}>
                <button style={S.btn} onClick={() => setPreviewMode("rendered")}>Rendered</button>
                <button style={S.btn} onClick={() => setPreviewMode("raw")}>Raw</button>
              </div>
            </div>
            {previewMode === "raw" ? <code style={{ fontSize: 13 }}>{noteContent || "(empty)"}</code> : <SafeText>{noteContent}</SafeText>}
          </div>
        )}
      </div>
      </div>
    </div>
  );
}
