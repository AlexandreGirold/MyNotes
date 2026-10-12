import { useCallback, useEffect, useState } from "react";
import { api as jsonApi, downloadFile } from "../api";
import type { Block, Job, JobBrief, TabProps, Deck } from "../types";

interface MaterialOut {
  mode: string;
  blocks: Block[];
  warnings: string[];
  estimated_tokens: number;
}

interface SlidesProps extends TabProps {
}

const TERMINAL = new Set(["succeeded", "failed"]);

function splitSlides(markdown: string): string[] {
  const regex = new RegExp("^---\\s*$", "m");
  return markdown.split(regex).map((s) => s.trim()).filter(Boolean);
}

function renderSlideContent(markdown: string): React.ReactNode {
  const lines = markdown.split("\n");
  const elements: React.ReactNode[] = [];
  let key = 0;
  for (const line of lines) {
    const headingMatch = line.match(/^(#{1,6})\s+(.*)/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      elements.push(<Tag key={key++}>{headingMatch[2]}</Tag>);
    } else if (/^---\s*$/.test(line)) {
      elements.push(<hr key={key++} />);
    } else if (line.trim()) {
      elements.push(<p key={key++}>{line}</p>);
    }
  }
  return elements;
}

export default function Slides({ selectedIds, onJob }: SlidesProps) {
  const [decks, setDecks] = useState<Deck[]>([]);
  const [currentDeck, setCurrentDeck] = useState<Deck | null>(null);
  const [editTitle, setEditTitle] = useState("Untitled");
  const [editMarkdown, setEditMarkdown] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [topic, setTopic] = useState("");
  const [sources, setSources] = useState<Block[]>([]);
  const [slideCount, setSlideCount] = useState(5);
  const [language, setLanguage] = useState("en");
  const [generating, setGenerating] = useState(false);
  const [genJob, setGenJob] = useState<JobBrief | null>(null);
  const [exportJob, setExportJob] = useState<JobBrief | null>(null);
  const [exporting, setExporting] = useState(false);
  type PreviewMode = "edit" | "rendered" | "iframe";
  const [previewMode, setPreviewMode] = useState<PreviewMode>("edit");
  const [iframeUrl, setIframeUrl] = useState("");
  useEffect(() => () => { if (iframeUrl) URL.revokeObjectURL(iframeUrl); }, [iframeUrl]);

  useEffect(() => {
    let cancelled = false; setLoading(true);
    jsonApi<Deck[]>("/decks").then((d) => { if (!cancelled) setDecks(d); }).catch(() => {}).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const handleFetchMaterial = useCallback(async () => {
    if (!topic.trim() || selectedIds.length === 0) return; setError(""); setLoading(true);
    try {
      const r = await jsonApi<MaterialOut>("/material", { method: "POST", body: JSON.stringify({ document_ids: selectedIds, query: topic }) });
      setSources(r.blocks);
    } catch (e) { setError(e instanceof Error ? e.message : "Failed"); } finally { setLoading(false); }
  }, [selectedIds, topic]);

  const pollJob = useCallback((jobId: string): Promise<JobBrief> => {
    let done = false;
    return new Promise((resolve, reject) => {
      const tick = async () => {
        if (done) return;
        try {
          const job = await jsonApi<Job>(`/jobs/${jobId}`);
          if (TERMINAL.has(job.status)) { done = true; if (job.status === "failed") reject(new Error(job.error || "Failed")); else resolve(job); }
          else setTimeout(tick, 1000);
        } catch { done = true; reject(new Error("Polling failed")); }
      };
      setTimeout(tick, 500);
    });
  }, []);

  const handleGenerate = useCallback(async () => {
    if (sources.length === 0 && selectedIds.length === 0) { setError("No material selected"); return; }
    setError(""); setGenerating(true);
    try {
      const result = await jsonApi<Job>("/slide-drafts", { method: "POST", body: JSON.stringify({ document_ids: selectedIds, topic, count: slideCount, language }) });
      setGenJob(result); onJob(result);
      const job = await pollJob(result.id); setGenJob(job);
      if (job.status === "succeeded" && job.result) {
        const d = (job.result as { deck?: { id?: string; title: string; markdown: string; source_ids?: string[] } }).deck;
        if (d) {
          const deck: Deck = { id: d.id || "", title: d.title, markdown: d.markdown, source_ids: d.source_ids || [], revision: 1, created_at: Date.now() / 1000, updated_at: Date.now() / 1000 };
          setCurrentDeck(deck); setEditTitle(deck.title); setEditMarkdown(deck.markdown); setDecks((prev) => [deck, ...prev]);
        }
      }
    } catch (e) { setError(e instanceof Error ? e.message : "Generation failed"); }
    finally { setGenerating(false); }
  }, [sources, selectedIds, topic, slideCount, language, onJob, pollJob]);

  const handleSave = useCallback(async () => {
    setError(""); setLoading(true);
    try {
      const payload = { title: editTitle, markdown: editMarkdown, source_ids: sources.length ? sources.map((s) => s.id) : currentDeck?.source_ids ?? [] };
      if (currentDeck) {
        const saved = await jsonApi<Deck>(`/decks/${currentDeck.id}`, { method: "PUT", body: JSON.stringify({ ...payload, expected_revision: currentDeck.revision }) });
        setCurrentDeck(saved); setEditTitle(saved.title); setEditMarkdown(saved.markdown); setDecks((prev) => prev.map((d) => (d.id === saved.id ? saved : d)));
      } else {
        const saved = await jsonApi<Deck>("/decks", { method: "POST", body: JSON.stringify(payload) });
        setCurrentDeck(saved); setEditTitle(saved.title); setEditMarkdown(saved.markdown); setDecks((prev) => [saved, ...prev]);
      }
    } catch (e) { setError(e instanceof Error ? e.message : "Save failed"); }
    finally { setLoading(false); }
  }, [currentDeck, editTitle, editMarkdown, sources]);

  const handleExport = useCallback(async () => {
    if (!currentDeck) return; setError(""); setExporting(true);
    try {
      const result = await jsonApi<Job>("/slide-exports", { method: "POST", body: JSON.stringify({ deck_id: currentDeck.id, revision: currentDeck.revision }) });
      setExportJob(result); onJob(result);
      const job = await pollJob(result.id); setExportJob(job);
      if (job.status === "succeeded" && job.result) {
        const r = job.result as Record<string, string>;
        if (r.html_path) setIframeUrl(URL.createObjectURL(await downloadFile(r.html_path)));
      }
    } catch (e) { setError(e instanceof Error ? e.message : "Export failed"); }
    finally { setExporting(false); }
  }, [currentDeck, onJob, pollJob]);

  const slides = splitSlides(editMarkdown);

  const S: Record<string, React.CSSProperties> = {
    panel: { display: "flex", gap: 12, height: "100%" },
    sidebar: { width: 200, flexShrink: 0, borderRight: "1px solid #ddd", paddingRight: 12, overflow: "auto" },
    main: { flex: 1, display: "flex", flexDirection: "column", gap: 8, minWidth: 0 },
    fieldset: { border: "1px solid #ddd", borderRadius: 6, padding: 12 },
    input: { padding: "4px 8px", border: "1px solid #ccc", borderRadius: 4, fontSize: 13, marginRight: 8 },
    textarea: { fontFamily: "monospace", fontSize: 13, width: "100%", padding: 8, border: "1px solid #ccc", borderRadius: 4, resize: "vertical", boxSizing: "border-box", minHeight: 200 },
    btnPrimary: { background: "#3b82f6", color: "#fff", border: "none", borderRadius: 4, padding: "6px 16px", cursor: "pointer", fontWeight: "bold", fontSize: 13 },
    btnSecondary: { background: "#f3f4f6", color: "#111", border: "1px solid #d1d5db", borderRadius: 4, padding: "6px 12px", cursor: "pointer", fontSize: 13 },
    alertError: { background: "#fef2f2", color: "#b91c1c", padding: "8px 12px", borderRadius: 4, fontSize: 13, border: "1px solid #fecaca" },
  };

  return (
    <div style={S.panel}>
      {error && <div style={S.alertError} role="alert">{error}</div>}
      <div style={S.sidebar}>
        <h3 style={{ margin: "0 0 8px", fontSize: 15 }}>Decks</h3>
        {loading && decks.length === 0 ? <p style={{ fontSize: 13, color: "#888" }}>Loading...</p> : null}
        <ul style={{ listStyle: "none", padding: 0, margin: 0, maxHeight: 400, overflow: "auto" }}>
          {decks.map((d) => (
            <li key={d.id} onClick={() => { setCurrentDeck(d); setEditTitle(d.title); setEditMarkdown(d.markdown); setPreviewMode("edit" as PreviewMode); }}
              style={{ padding: "4px 8px", cursor: "pointer", borderRadius: 4, background: currentDeck?.id === d.id ? "#e0e7ff" : "transparent", fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {d.title}
            </li>
          ))}
        </ul>
        <button onClick={() => { setCurrentDeck(null); setEditTitle("Untitled"); setEditMarkdown(""); }} style={S.btnPrimary}>+ New Deck</button>
      </div>
      <div style={S.main}>
        <input type="text" value={editTitle} onChange={(e) => setEditTitle(e.target.value)} placeholder="Deck title"
          style={{ fontSize: 20, fontWeight: "bold", border: "none", borderBottom: "1px solid #ccc", paddingBottom: 4, outline: "none", marginBottom: 4, width: "100%" }} />
        <fieldset style={S.fieldset}>
          <legend>Source Material</legend>
          <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
            <input type="text" value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="Topic query..."
              style={{ ...S.input, flex: 1, minWidth: 160 }} />
            <button onClick={handleFetchMaterial} disabled={!topic.trim() || selectedIds.length === 0 || loading}>Fetch Sources</button>
          </div>
          {sources.map((s) => (
            <li key={s.id} style={{ marginBottom: 6, fontSize: 13 }}>
              <strong>{s.title}</strong> [{s.id.slice(0, 8)}]
              <p style={{ margin: "2px 0 0", whiteSpace: "pre-wrap" }}>{s.text.slice(0, 200)}{s.text.length > 200 ? "..." : ""}</p>
            </li>
          ))}
        </fieldset>
        <fieldset style={S.fieldset}>
          <legend>Generate Draft</legend>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="number" value={slideCount} onChange={(e) => setSlideCount(Number(e.target.value) || 1)} min={1} max={50} style={{ ...S.input, width: 60 }} />
            <select value={language} onChange={(e) => setLanguage(e.target.value)} style={S.input}>
              <option value="en">English</option><option value="fr">Français</option><option value="de">Deutsch</option><option value="es">Español</option><option value="pt">Português</option>
            </select>
            <button onClick={handleGenerate} disabled={generating || (sources.length === 0 && selectedIds.length === 0)}>
              {generating ? "Generating..." : "Generate"}
            </button>
          </div>
          {genJob && <div style={{ fontSize: 12, color: "#555", marginTop: 4 }}>{genJob.status} — {Math.round(genJob.progress * 100)}%</div>}
        </fieldset>
        <div style={S.fieldset}>
          <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
            {(previewMode === "iframe" && iframeUrl ? ["edit", "rendered", "iframe"] as const : ["edit", "rendered"] as const).map((m) => (
              <button key={m} onClick={() => setPreviewMode(m as PreviewMode)} style={{ ...S.btnSecondary, fontWeight: previewMode === m ? "bold" : "normal" }}>
                {m === "edit" ? "Edit" : m === "rendered" ? "Rendered" : "Sandbox"}
              </button>
            ))}
          </div>
          {previewMode === "edit" && (
            <textarea value={editMarkdown} onChange={(e) => setEditMarkdown(e.target.value)}
              placeholder="Marp markdown - # headings, --- slide breaks" style={S.textarea} />
          )}
          {previewMode === "rendered" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              {slides.length === 0 ? <p style={{ color: "#888", fontSize: 13 }}>No slides yet</p> :
                slides.map((slide, i) => (
                  <div key={i} style={{ border: "1px solid #ddd", borderRadius: 6, padding: 24, background: "#fff" }}>
                    {renderSlideContent(slide)}
                  </div>
                ))}
            </div>
          )}
          {previewMode === "iframe" && iframeUrl && (
            <iframe src={iframeUrl} sandbox="" title="Exported slide preview"
              style={{ width: "100%", height: 500, border: "1px solid #ccc", borderRadius: 4 }} />
          )}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
          <button onClick={handleSave} disabled={loading} style={S.btnPrimary}>Save</button>
          {currentDeck && <button onClick={handleExport} disabled={exporting} style={S.btnSecondary}>Export</button>}
          {exportJob && <span style={{ fontSize: 12, color: "#555" }}>Export: {exportJob.status}</span>}
          {exportJob?.result && Object.entries(exportJob.result).filter(([key]) => key.endsWith("_path")).map(([key, value]) => (
            <button key={key} onClick={async () => {
              const url = URL.createObjectURL(await downloadFile(String(value)));
              const link = document.createElement("a"); link.href = url; link.download = String(value).split("/").pop() || key;
              link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
            }}>Download {key.replace("_path", "")}</button>
          ))}
        </div>
      </div>
    </div>
  );
}
