import { useCallback, useEffect, useRef, useState } from "react";

import Notes from "./components/Notes";
import Chat from "./components/Chat";
import LiveNotes from "./components/LiveNotes";
import Settings from "./components/Settings";
import SourceViewer from "./components/SourceViewer";
import ThemeToggle from "./components/ThemeToggle";

import { api as jsonApi, downloadFile as fetchFile } from "./api";
import type {
   Block, Citation, Course, DocumentSummary, Job, JobBrief, Version,
} from "./types";

type TabId = "notes" | "chat" | "recording" | "settings" | "sourceviewer" | "courses";
const navIcons: Partial<Record<TabId, string>> = { notes: "▤", chat: "✦", recording: "◉", courses: "▥", settings: "⚙" };

export default function App() {

  const [jobs, setJobs] = useState<Record<string, JobBrief>>({});
  const [courses, setCourses] = useState<Course[]>([]);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selectedCourseId, setSelectedCourseId] = useState("");
  const [activeTab, setActiveTab] = useState<TabId>("recording");
  const [selectedDoc, setSelectedDoc] = useState<DocumentSummary | null>(null);
  const [version, setVersion] = useState<Version | null>(null);
  const [sourceBlock, setSourceBlock] = useState<Block | null>(null);
  const [noteFormat, setNoteFormat] = useState<"markdown" | "typst">("markdown");
  const [noteTitle, setNoteTitle] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [noteCourse, setNoteCourse] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<Block[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [liveStatus, setLiveStatus] = useState("idle");
  const polling = useRef(new Set<string>());
  const pollTimers = useRef(new Set<ReturnType<typeof setTimeout>>());
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; pollTimers.current.forEach(clearTimeout); polling.current.clear();
  }; }, []);

  /* ── Job polling ─────────────────────────────────────────────── */

  const pollJob = useCallback((jobId: string) => {
    if (!jobId || polling.current.has(jobId)) return;
    polling.current.add(jobId);
    const schedule = (delay: number) => {
      const timer = setTimeout(() => { pollTimers.current.delete(timer); tick(); }, delay);
      pollTimers.current.add(timer);
    };
    let done = false;
    const tick = async () => {
      if (done || !mounted.current) return;
      try {
        const job = await jsonApi<Job>(`/jobs/${jobId}`);
        setJobs((s) => ({ ...s, [job.id]: job }));
        if (TERMINAL.has(job.status)) { done = true; polling.current.delete(jobId); } else { schedule(1000); }
      } catch { done = true; polling.current.delete(jobId); }
    };
    schedule(500);
  }, []);

  /* ── Load courses ────────────────────────────────────────────── */

  useEffect(() => {
    jsonApi<Course[]>("/courses").then(setCourses).catch(() => {});
  }, []);

  /* ── Load documents ──────────────────────────────────────────── */

  const loadDocuments = useCallback(async (courseId?: string) => {
    try {
      const docs = courseId ? await jsonApi<DocumentSummary[]>(`/documents?course_id=${courseId}`) : await jsonApi<DocumentSummary[]>("/documents");
      setDocuments(docs); setSelectedIds([]); setSelectedDoc(null); setVersion(null); setSourceBlock(null);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { loadDocuments(selectedCourseId || undefined); }, [loadDocuments, selectedCourseId]);
  useEffect(() => {
    if (Object.values(jobs).some(job => job.status === "succeeded")) {
      jsonApi<DocumentSummary[]>(selectedCourseId ? `/documents?course_id=${selectedCourseId}` : "/documents").then(setDocuments).catch(() => {});
    }
  }, [jobs, selectedCourseId]);

  /* ── Load settings ───────────────────────────────────────────── */


  /* ── Selected document details ───────────────────────────────── */

  const loadDocumentDetails = useCallback(async (docId: string) => {
    try {
      const doc = await jsonApi<DocumentSummary>(`/documents/${docId}`);
      setSelectedDoc(doc);
      if (doc.current_version_id) {
        const ver = await jsonApi<Version>(`/versions/${doc.current_version_id}`);
        setVersion(ver);
        if (["markdown", "md", "typst", "typ"].includes(doc.kind) && ver.original_path) {
          setNoteContent(await (await fetchFile(ver.original_path)).text());
          setNoteFormat(["typst", "typ"].includes(doc.kind) ? "typst" : "markdown");
          setNoteCourse(doc.course_id ?? "");
        }
      }
    } catch { /* ignore */ }
  }, []);

  /* ── Course CRUD ─────────────────────────────────────────────── */

  const createCourse = useCallback(async (name: string) => {
    const course = await jsonApi<Course>("/courses", { method: "POST", body: JSON.stringify({ name }) });
    setCourses((s) => [...s, course]);
    return course;
  }, []);

  const deleteCourse = useCallback(async (id: string) => {
    await jsonApi(`/courses/${id}`, { method: "DELETE" });
    setCourses((s) => s.filter((c) => c.id !== id));
  }, []);

  /* ── Note CRUD ───────────────────────────────────────────────── */

  const saveNote = useCallback(async (title: string, content: string, format: string, courseId?: string) => {
    const job = await jsonApi<Job>("/notes", { method: "POST", body: JSON.stringify({ title, content, format, course_id: courseId }) });
    pollJob(job.id);
    setJobs((s) => ({ ...s, [job.id]: job }));
    return job;
  }, [pollJob]);

  const updateNote = useCallback(async (noteId: string, title: string, content: string, format: string, courseId?: string) => {
    const job = await jsonApi<Job>(`/notes/${noteId}`, { method: "PUT", body: JSON.stringify({ title, content, format, course_id: courseId }) });
    pollJob(job.id);
    setJobs((s) => ({ ...s, [job.id]: job }));
    return job;
  }, [pollJob]);
  const saveDocumentMetadata = useCallback(async (id: string, title: string, course: string, chapter: string, class_date: string) => {
    await jsonApi(`/documents/${id}`, { method: "PATCH", body: JSON.stringify({ title, course_id: course || null, chapter, class_date }) });
    await loadDocumentDetails(id);
    const list = await jsonApi<DocumentSummary[]>(selectedCourseId ? `/documents?course_id=${selectedCourseId}` : "/documents");
    setDocuments(list);
  }, [loadDocumentDetails, selectedCourseId]);
  const deleteDocument = useCallback(async (id: string) => {
    await jsonApi(`/documents/${id}`, { method: "DELETE" });
    setDocuments(list => list.filter(doc => doc.id !== id));
    setSelectedIds(ids => ids.filter(value => value !== id));
    if (selectedDoc?.id === id) { setSelectedDoc(null); setVersion(null); setNoteTitle(""); setNoteContent(""); }
  }, [selectedDoc]);

  /* ── Search ──────────────────────────────────────────────────── */

  const doSearch = useCallback(async (q: string, courseId?: string) => {
    if (!q.trim()) { setSearchResults([]); return; }
    setSearchLoading(true);
    try {
      const params = new URLSearchParams({ q });
      if (courseId) params.set("course_id", courseId);
      setSearchResults(await jsonApi<Block[]>(`/search?${params}`));
    } catch { setSearchResults([]); }
    finally { setSearchLoading(false); }
  }, []);

  /* ── Toggle document selection ───────────────────────────────── */

  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((s) => {
      const exists = s.includes(id);
      return exists ? s.filter((i) => i !== id) : [...s, id];
    });
    setSelectedDoc((prev) => {
      if (prev?.id === id) return null;
      const doc = documents.find((d) => d.id === id);
      return doc || null;
    });
  }, [documents]);

  /* ── Download file ───────────────────────────────────────────── */

  const downloadFile = useCallback(async (path: string) => {
    try {
      const blob = await fetchFile(path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = path.split("/").pop() || "download";
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch { /* */ }
  }, []);

  /* ── Source click handler ────────────────────────────────────── */

  const handleSourceClick = useCallback((source: Block | Citation) => {
    if (source.version_id) {
      setSourceBlock({ ...source, id: "source_id" in source ? source.source_id : source.id,
        text: "excerpt" in source ? source.excerpt : source.text } as Block);
    } else {
      setSourceBlock({ id: (source as Citation).source_id, text: (source as Citation).excerpt, version_id: "", document_id: "", title: "" });
    }
    setActiveTab("sourceviewer");
  }, []);

  /* ── Persisted jobs polling ──────────────────────────────────── */

  useEffect(() => {
    const pollAll = async () => {
      try {
        const jobs = await jsonApi<Job[]>("/jobs");
        jobs.filter(j => j.status === "queued" || j.status === "running").forEach((j) => pollJob(j.id));
      } catch { /* */ }
    };
    pollAll();
    const id = setInterval(pollAll, 10000);
    return () => clearInterval(id);
  }, [pollJob]);

  /* ── Shared job handler ──────────────────────────────────────── */

  const sharedJobHandler = useCallback((job: JobBrief) => setJobs((s) => ({ ...s, [job.id]: job })), []);

  /* ── Tabs ────────────────────────────────────────────────────── */

  const tabs: { id: TabId; label: string }[] = [
    { id: "notes", label: "Notes & files" }, { id: "chat", label: "AI assistant" },
    { id: "recording", label: "Live notes" },
    { id: "courses", label: "Courses" }, { id: "settings", label: "Settings" },
  ];

  /* ── Render ──────────────────────────────────────────────────── */

  return (
    <div className="workspace">
      <aside className="workspace-sidebar"><div className="workspace-brand"><span className="brand-icon">M</span><div><strong>MyNotes</strong><small>Your personal workspace</small></div></div>
      <div className="sidebar-label">WORKSPACE</div>
      <nav className="tab-bar" role="tablist" aria-label="Main navigation">
        {tabs.map((t) => (
          <button key={t.id} className={`tab-btn ${activeTab === t.id ? "active" : ""}`}
            onClick={() => setActiveTab(t.id)} role="tab"
            aria-selected={activeTab === t.id}><span className="nav-icon">{navIcons[t.id]}</span>{t.label}{t.id === "recording" && <small className={`nav-new ${liveStatus === "live" ? "is-live" : ""}`}>{liveStatus === "live" ? "LISTENING" : "LIVE"}</small>}</button>
        ))}
      </nav>
      <div className="sidebar-footer"><span>Local library · Cloud Temple AI</span><small>Your notes stay in your workspace.</small></div></aside>
      <div className="workspace-main"><header className="workspace-topbar"><span>My workspace <span className="breadcrumb-divider">/</span> <strong>{tabs.find(tab => tab.id === activeTab)?.label || "Source"}</strong></span><div className="button-row">{liveStatus === "live" && <button className="text-button" onClick={() => setActiveTab("recording")}>● Transcript running</button>}<span className="workspace-count">{documents.length} document{documents.length === 1 ? "" : "s"}</span><ThemeToggle /></div></header>
      <main className={`tab-content view-${activeTab}`} role="tabpanel">
        {activeTab === "notes" && <div className="page-heading"><div><div className="page-eyebrow">YOUR KNOWLEDGE, TOGETHER</div><h1>Notes & files</h1></div><span className="muted">Write, collect and organize.</span></div>}
        {activeTab === "notes" && <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <button onClick={() => { setSelectedIds([]); setSelectedDoc(null); setVersion(null); setNoteTitle(""); setNoteContent(""); }}>New note</button>
          <button onClick={async () => {
            try {
              const file = await window.mynotes.selectImport();
              if (!file) return;
              const body = new FormData();
              body.append("file", new Blob([file.data]), file.name);
              if (file.kind) body.append("kind", file.kind);
              if (file.name.endsWith(".zip")) body.append("entrypoint", window.prompt("Typst project entry point", "main.typ") || "main.typ");
              const job = await jsonApi<Job>("/imports", { method: "POST", body });
              setJobs(s => ({ ...s, [job.id]: job }));
              pollJob(job.id);
            } catch (e) { window.alert(e instanceof Error ? e.message : "Import failed"); }
          }}>Import document</button>
        </div>}
        {Object.values(jobs).filter(job => job.status === "failed").map(job => (
          <p role="alert" key={job.id}>{job.error || "Job failed"} <button onClick={async () => {
            const retry = await jsonApi<Job>(`/jobs/${job.id}/retry`, { method: "POST" });
            setJobs(s => ({ ...s, [job.id]: retry })); pollJob(job.id);
          }}>Retry</button></p>
        ))}
        {activeTab === "notes" && (
          <Notes selectedIds={selectedIds} onJob={sharedJobHandler} jobs={jobs}
            noteFormat={noteFormat} noteTitle={noteTitle}   noteContent={noteContent} noteCourse={noteCourse}
            setNoteFormat={setNoteFormat} setNoteTitle={setNoteTitle}
            setNoteContent={setNoteContent} setNoteCourse={setNoteCourse}
            selectedDoc={selectedDoc} version={version}
            saveNote={saveNote} updateNote={updateNote}
            saveMetadata={saveDocumentMetadata}
            loadDocumentDetails={loadDocumentDetails}
            toggleSelect={toggleSelect} documents={documents}
            searchQuery={searchQuery} setSearchQuery={setSearchQuery}
            doSearch={doSearch} searchResults={searchResults} searchLoading={searchLoading}
            courses={courses} downloadFile={downloadFile}
            deleteDocument={deleteDocument}
            selectedCourseId={selectedCourseId} onCourseFilterChange={setSelectedCourseId}
            onSource={handleSourceClick} />
        )}
        {activeTab === "chat" && (
          <Chat selectedIds={selectedIds} onJob={sharedJobHandler} onSource={handleSourceClick} onSettings={() => setActiveTab("settings")} />
        )}
        <div className="live-notes-view" style={{ display: activeTab === "recording" ? "block" : "none" }}><LiveNotes active={activeTab === "recording"} onStateChange={setLiveStatus} onJob={sharedJobHandler} onSettings={() => setActiveTab("settings")} onSaved={() => {
          jsonApi<DocumentSummary[]>("/documents").then(setDocuments).catch(() => {});
        }} /></div>
        {activeTab === "courses" && (
          <div style={{ maxWidth: 600, margin: "0 auto" }}>
            <h2 style={{ margin: "0 0 16px" }}>Courses</h2>
            <CourseManager courses={courses} documents={documents}
              onCreateCourse={(name) => createCourse(name).then(() => loadDocuments())}
              onDeleteCourse={deleteCourse} onSelectCourse={(id) => { setSelectedCourseId(id); setActiveTab("notes"); }} />
          </div>
        )}
        {activeTab === "settings" && <Settings onJob={sharedJobHandler} />}
        {activeTab === "sourceviewer" && (
        <SourceViewer source={sourceBlock} onClose={() => setActiveTab("notes")} />
        )}
      </main></div>
    </div>
  );
}

const TERMINAL = new Set(["succeeded", "failed"]);

/* ── Inline courses manager (tiny) ─────────────────────────────── */

interface CourseManagerProps {
  courses: Course[]; documents: DocumentSummary[];
  onCreateCourse: (name: string) => Promise<void>;
  onDeleteCourse: (id: string) => Promise<void>;
  onSelectCourse: (id: string) => void;
}

function CourseManager({ courses, documents, onCreateCourse, onDeleteCourse, onSelectCourse }: CourseManagerProps) {
  const [newName, setNewName] = useState("");
  const [error, setError] = useState("");
  const btn: React.CSSProperties = { padding: "4px 12px", borderRadius: 4, border: "1px solid #ccc", background: "#fff", cursor: "pointer", fontSize: 12 };
  return (
    <>
      {error && <div className="error-banner" role="alert">{error}</div>}
      <form onSubmit={async (e) => { e.preventDefault(); if (!newName.trim()) return; try { await onCreateCourse(newName.trim()); setNewName(""); setError(""); } catch (err) { setError(err instanceof Error ? err.message : "Failed"); } }}
        style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <input type="text" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="New course name"
          style={{ flex: 1, padding: 8, borderRadius: 4, border: "1px solid #ccc" }} aria-label="Course name" />
        <button type="submit" style={{ padding: "8px 16px", borderRadius: 4, border: "none", background: "#2563eb", color: "#fff", cursor: "pointer" }}>Add Course</button>
      </form>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {courses.map((c) => (
          <li key={c.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 12px", borderRadius: 4, marginBottom: 4, background: "#fff", border: "1px solid #ddd" }}>
            <span onClick={() => onSelectCourse(c.id)} style={{ cursor: "pointer", flex: 1 }} title={`${documents.filter((d) => d.course_id === c.id).length} document(s)`}>{c.name}</span>
            <button onClick={() => onDeleteCourse(c.id).catch((e) => setError(e instanceof Error ? e.message : "Failed"))} style={{ ...btn, color: "#dc2626", marginLeft: 8 }}>Delete</button>
          </li>
        ))}
        {courses.length === 0 && <p style={{ color: "#888" }}>No courses yet.</p>}
      </ul>
    </>
  );
}
