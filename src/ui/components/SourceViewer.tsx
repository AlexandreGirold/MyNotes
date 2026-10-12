import { useEffect, useRef, useState } from "react";
import { api, downloadFile, secureFileUrl } from "../api";
import * as pdfjs from "pdfjs-dist";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { PDFDocumentProxy } from "pdfjs-dist/types/src/pdf";
import type { Version } from "../types";
pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

export default function SourceViewer({ source, onClose }: { source: any; onClose: () => void }) {
  const [version, setVersion] = useState<Version | null>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [text, setText] = useState("");
  const [audio, setAudio] = useState("");
  const [images, setImages] = useState<{ url: string; title: string }[]>([]);
  const [error, setError] = useState("");
  const [visualStatus, setVisualStatus] = useState("");
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let stopped = false;
    let doc: PDFDocumentProxy | null = null;
    const urls: string[] = [];
    setVersion(null); setPdf(null); setAudio(""); setImages([]); setError("");
    if (!source?.version_id) { setError("This citation has no resolvable version."); return; }
    (async () => {
      const v = await api<Version>(`/versions/${source.version_id}`);
      if (stopped) return;
      setVersion(v);
      const assets = v.assets || [];
      const pdfPath = assets.find(a => a.path.endsWith(".pdf"))?.path || (v.original_path?.endsWith(".pdf") ? v.original_path : "");
      if (pdfPath) {
        doc = await pdfjs.getDocument({ data: await (await downloadFile(pdfPath)).arrayBuffer() }).promise;
        if (stopped) { await doc.loadingTask.destroy(); return; }
        setPdf(doc); setPage(Math.max(1, Math.min(doc.numPages, Number(source.locator?.page) || 1)));
      }
      if (v.original_path && /\.(webm|wav|mp3|ogg|flac|m4a)$/i.test(v.original_path)) {
        const url = await secureFileUrl(v.original_path); urls.push(url);
        if (!stopped) setAudio(url);
      }
      const loaded = [];
      for (const asset of assets.filter(a => /\.(png|jpe?g|webp)$/i.test(a.path))) {
        const url = await secureFileUrl(asset.path); urls.push(url);
        loaded.push({ url, title: asset.title || asset.path.split("/").pop() || "Extracted picture" });
      }
      if (!stopped) setImages(loaded);
    })().catch(e => { if (!stopped) setError(e.message); });
    return () => { stopped = true; doc?.loadingTask.destroy(); urls.forEach(url => URL.revokeObjectURL(url)); };
  }, [source?.version_id]);

  useEffect(() => {
    if (!pdf || !canvas.current) return;
    let stopped = false;
    let task: ReturnType<pdfjs.PDFPageProxy["render"]> | undefined;
    (async () => {
      const p = await pdf.getPage(page);
      if (stopped || !canvas.current) return;
      const viewport = p.getViewport({ scale: 1.3 });
      canvas.current.width = viewport.width; canvas.current.height = viewport.height;
      task = p.render({ canvas: canvas.current, viewport });
      await task.promise;
      const content = await p.getTextContent();
      if (!stopped) setText(content.items.map(item => "str" in item ? item.str : "").join(" "));
    })().catch(e => { if (!stopped) setError(e.message); });
    return () => { stopped = true; task?.cancel(); };
  }, [pdf, page]);

  if (!source) return null;
  return <section aria-label="Source viewer" style={{ maxWidth: 1000, margin: "auto" }}>
    <button onClick={onClose}>Close source</button>
    <h2>{source.title || "Immutable source"}</h2>
    <p>Version: <code>{source.version_id}</code> · {JSON.stringify(source.locator)}</p>
    {error && <p role="alert">{error}</p>}
    {version?.warnings?.map((warning, i) => <p key={i}>{warning}</p>)}
    <pre style={{ whiteSpace: "pre-wrap" }}>{source.text || source.excerpt}</pre>
    {pdf && <>
      <button disabled={page <= 1} onClick={() => setPage(p => p - 1)}>Previous page</button>
      <span> Page {page} / {pdf.numPages} </span>
      <button disabled={page >= pdf.numPages} onClick={() => setPage(p => p + 1)}>Next page</button>
      <canvas ref={canvas} style={{ maxWidth: "100%" }} />
      <pre style={{ whiteSpace: "pre-wrap" }}>{text}</pre>
      <button onClick={async () => {
        try {
          setVisualStatus("Queued…");
          const ack = await api<{ id: string }>("/visuals", { method: "POST", body: JSON.stringify({ version_id: version!.id, page, question: "Explain this page's diagrams and tables." }) });
          for (let i = 0; i < 120; i++) {
            await new Promise(resolve => setTimeout(resolve, 1000));
            const job = await api<any>(`/jobs/${ack.id}`);
            if (job.status === "failed") throw new Error(job.error);
            if (job.status === "succeeded") { setVisualStatus(job.result.description || job.result.warnings?.join(" ")); return; }
            setVisualStatus(job.status);
          }
          setVisualStatus("Still processing; check Jobs.");
        } catch (e) { setVisualStatus(e instanceof Error ? e.message : "Visual analysis failed"); }
      }}>Describe this page with AI (provider usage)</button>
      {visualStatus && <p>Generated interpretation: {visualStatus}</p>}
    </>}
    {audio && <audio controls src={audio} onLoadedMetadata={e => {
      const start = source.locator?.start;
      if (typeof start === "number" && Number.isFinite(start)) e.currentTarget.currentTime = start;
    }} />}
    {images.map(image => <figure key={image.url}><img src={image.url} alt={image.title} style={{ maxWidth: "100%" }} /><figcaption>{image.title}</figcaption></figure>)}
    {version?.original_path && <button onClick={async () => {
      const url = URL.createObjectURL(await downloadFile(version.original_path!));
      const a = document.createElement("a"); a.href = url; a.download = version.original_path!.split("/").pop() || "source"; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }}>Download original</button>}
  </section>;
}
