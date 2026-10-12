import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

type Seg = { start: number; end: number };
type Bridge = {
  saveRecordingChunk(s: string, i: number, d: ArrayBuffer): Promise<void>;
  finishRecording(s: string): Promise<{ name: string; data: ArrayBuffer }>;
};
export interface RecordingProps { onJob: (job: any) => void; }

export default function Recording({ onJob }: RecordingProps) {
  const bridge = useRef<Bridge | null>(null);
  if (!bridge.current && typeof window !== "undefined"
      && (window as any).mynotes?.saveRecordingChunk) {
    const w = window as any;
    bridge.current = {
      saveRecordingChunk: (s, i, d) => w.mynotes!.saveRecordingChunk(s, i, d),
      finishRecording: (s) => w.mynotes!.finishRecording(s),
    };
  }

  const [recording, setRecording] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState("");
  const [jobStatus, setJobStatus] = useState("idle");
  const [duration, setDuration] = useState(0);
  const [playbackUrl, setPlaybackUrl] = useState("");
  const [segments, setSegments] = useState<Seg[]>([]);
  const [transcript, setTranscript] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);

  const mrRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sessionRef = useRef("");
  const indexRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const blobsRef = useRef<Blob[]>([]);
  const deadRef = useRef(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const queueRef = useRef<Promise<void>>(Promise.resolve());
  const pollIvRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => {
    deadRef.current = true;
    if (pollIvRef.current) clearInterval(pollIvRef.current);
    try { const mr = mrRef.current; if (mr && mr.state !== "inactive") mr.stop(); } catch { /* */ }
    const s = streamRef.current; if (s) s.getTracks().forEach(t => t.stop());
    if (timerRef.current) clearInterval(timerRef.current);
  }, []);

  const enqueue = useCallback((sid: string, blob: Blob) => {
    const index = indexRef.current++;
    queueRef.current = queueRef.current.then(async () => {
      if (deadRef.current || !bridge.current || sessionRef.current !== sid) return;
      await bridge.current!.saveRecordingChunk(sid, index, await blob.arrayBuffer());
    }).catch((e: any) => {
      if (sessionRef.current !== sid) return;
      setError(`Failed to save chunk: ${e?.message ?? String(e)}`);
      setRecording(false); deadRef.current = true;
      if (timerRef.current) clearInterval(timerRef.current);
      const mr = mrRef.current;
      if (mr && mr.state !== "inactive") mr.stop();
      streamRef.current?.getTracks().forEach(track => track.stop());
      streamRef.current = null;
    });
  }, []);
  useEffect(() => () => { if (playbackUrl) URL.revokeObjectURL(playbackUrl); }, [playbackUrl]);

  const start = useCallback(async () => {
    if (recording) return;
    deadRef.current = false;
    setError(null); setPlaybackUrl(""); setSegments([]); setTranscript(""); setWarnings([]);
    setJobId(""); setJobStatus("idle"); setDuration(0);
    blobsRef.current = [];
    queueRef.current = Promise.resolve();
    let stream: MediaStream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { autoGainControl: false } }); }
    catch (e: any) {
      setError(e?.name === "NotAllowedError"
        ? "Microphone access denied. Allow permissions and retry."
        : `Could not access mic: ${e?.message ?? String(e)}`);
      return;
    }
    streamRef.current = stream;
    sessionRef.current = Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    indexRef.current = 0;
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus" : MediaRecorder.isTypeSupported("audio/webm")
      ? "audio/webm" : "";
    const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
    mrRef.current = mr;
    const sid = sessionRef.current;
    mr.ondataavailable = (ev: BlobEvent) => {
      if (deadRef.current || !ev.data || ev.data.size === 0) return;
      blobsRef.current.push(ev.data);
      enqueue(sid, ev.data);
    };
    mr.start(1000);
    setRecording(true);
    timerRef.current = setInterval(() => setDuration(d => d + 1), 1000);
  }, [recording, enqueue]);

  const pollJob = useCallback((id: string) => {
    if (pollIvRef.current) clearInterval(pollIvRef.current);
    const tick = async () => {
      if (deadRef.current) return;
      try {
        const job = await api<any>(`/jobs/${id}`);
        setJobStatus(job.status); onJob(job);
        if (job.status === "succeeded" || job.status === "failed") {
          if (job.result?.segments) setSegments(job.result.segments as Seg[]);
          setTranscript(job.result?.text || "");
          setWarnings(job.result?.warnings || []);
          if (job.status === "failed") setError(job.error || "Transcription failed; your recording remains saved locally.");
          if (pollIvRef.current) clearInterval(pollIvRef.current);
        }
      } catch { /* */ }
    };
    tick();
    pollIvRef.current = setInterval(tick, 3000);
  }, [onJob]);

  const stopAndSubmit = useCallback(async () => {
    if (!sessionRef.current) return;
    setSubmitting(true);
    setRecording(false);
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    const mr = mrRef.current;
    if (mr && mr.state !== "inactive") {
      await new Promise<void>((resolve) => {
        mr.addEventListener("stop", () => resolve(), { once: true });
        mr.stop();
      });
    }
    mrRef.current = null;
    await queueRef.current;
    if (deadRef.current) { setSubmitting(false); return; }
    const stream = streamRef.current;
    if (stream) { stream.getTracks().forEach(t => t.stop()); streamRef.current = null; }
    if (blobsRef.current.length > 0)
      setPlaybackUrl(URL.createObjectURL(new Blob(blobsRef.current, { type: "audio/webm" })));
    blobsRef.current = [];
    const sid = sessionRef.current;
    if (bridge.current) {
      try {
        const result = await bridge.current.finishRecording(sid);
        const { name, data } = result;
        const fd = new FormData();
        fd.append("file", new Blob([data], { type: "audio/webm" }), name.replace(/\.\w+$/, "") + ".webm");
        fd.append("session_id", sid);
        const job = await api<any>("/recordings", { method: "POST", body: fd });
        const id = job.job_id ?? job.id;
        setJobId(id); setJobStatus(job.status ?? "queued"); onJob(job);
        pollJob(id);
      } catch (e: any) { setError(`Submit failed: ${e.message ?? String(e)}`); }
    } else { setJobStatus("local-only"); onJob({ id: "local", status: "local-only" }); }
    setSubmitting(false);
  }, [bridge, onJob, pollJob]);

  const playSegment = useCallback((seg: Seg) => {
    if (audioRef.current) audioRef.current.currentTime = seg.start;
  }, []);

  const downloadRecording = useCallback(() => {
    if (!playbackUrl) return;
    const a = document.createElement("a");
    a.href = playbackUrl; a.download = "recording.webm";
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
  }, [playbackUrl]);

  return (
    <div style={{ padding: 16, border: "1px solid #ccc", borderRadius: 8 }}>
      <h3 style={{ margin: "0 0 12px" }}>Recording</h3>
      {error && <div role="alert" style={{ color: "red", marginBottom: 8, padding: 8, background: "#fee" }}>{error}</div>}
      <div style={{ marginBottom: 8, display: "flex", gap: 8 }}>
        {!recording ? <button disabled={submitting} onClick={start}>{submitting ? "Saving…" : "Start Recording"}</button>
          : <button onClick={stopAndSubmit}>Stop &amp; Submit</button>}
      </div>
      {recording && <p>Recording... {duration}s</p>}
      {jobId && <p>Job <code>{jobId}</code>: {jobStatus}</p>}
      {warnings.map((warning, i) => <p key={i}>{warning}</p>)}
      {transcript && <pre style={{ whiteSpace: "pre-wrap" }}>{transcript}</pre>}
      {playbackUrl && <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <audio ref={audioRef} controls src={playbackUrl} style={{ flex: 1 }} />
        <button onClick={downloadRecording} style={{ padding: "4px 8px" }}>Download</button>
      </div>}
      {segments.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary>{segments.length} segment(s)</summary>
          <ul style={{ fontSize: 12 }}>
            {segments.map((s, i) => (
              <li key={i} onClick={() => playSegment(s)} style={{ cursor: "pointer" }}>
                {i}: {s.start.toFixed(1)}s → {s.end.toFixed(1)}s
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
