import { useCallback, useEffect, useRef, useState } from "react";
import { api, getConnection } from "../api";
import workletUrl from "../pcm-worklet.js?url";
import MarkdownEditor from "./MarkdownEditor";
import { REALTIME_MODEL_ID, selectRealtimeModel } from "../models";

type Props = { active: boolean; onJob: (job: any) => void; onSaved: (id: string) => void; onSettings: () => void; onStateChange?: (status: string) => void };
type NoteKind = "lecture" | "meeting";
type Lang = "auto" | "fr" | "en";
const NOTE_KEY = "mynotes-session-notes";
const TRANSCRIPT_KEY = "mynotes-live-transcript-recovery";
const formatTime = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

export default function LiveNotes({ active, onJob, onSaved, onSettings, onStateChange }: Props) {
  const [title, setTitle] = useState("Untitled session");
  const [status, setStatus] = useState("idle");
  const [text, setText] = useState(() => { try { return localStorage.getItem(TRANSCRIPT_KEY) || ""; } catch { return ""; } });
  const [notes, setNotes] = useState(() => { try { return localStorage.getItem(NOTE_KEY) || ""; } catch { return ""; } });
  const [rawText, setRawText] = useState("");
  const [showRaw, setShowRaw] = useState(false);
  const [reviewed, setReviewed] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [savedPath, setSavedPath] = useState("");
  const [error, setError] = useState("");
  const [docId, setDocId] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [micLevel, setMicLevel] = useState(0);
  const [model, setModel] = useState(REALTIME_MODEL_ID);
  const [kind, setKind] = useState<NoteKind>("lecture");
  const [lang, setLang] = useState<Lang>("fr");
  const [glossary, setGlossary] = useState("");
  const [hasKey, setHasKey] = useState(false);
  const [speakerReady, setSpeakerReady] = useState(false);
  const [detectSpeakers, setDetectSpeakers] = useState(false);
  const [numSpeakers, setNumSpeakers] = useState(0);
  const [speakerText, setSpeakerText] = useState("");
  const [speakerDocId, setSpeakerDocId] = useState("");
  const [showSpeakers, setShowSpeakers] = useState(false);
  const [speakerJobId, setSpeakerJobId] = useState("");
  const [setupStage, setSetupStage] = useState("");
  const speakerJob = useRef("");
  const ws = useRef<WebSocket | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const ctx = useRef<AudioContext | null>(null);
  const proc = useRef<AudioWorkletNode | null>(null);
  const finish = useRef<(id: string) => void>(() => {});
  const flushed = useRef<() => void>(() => {});
  const finalId = useRef("");
  const alive = useRef(true);
  const statusRef = useRef("idle");
  const notesRef = useRef(notes); notesRef.current = notes;
  const abortCtl = useRef<AbortController | null>(null);
  const textRef = useRef(text); textRef.current = text;
  const pendingText = useRef("");
  const renderTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flushText = useCallback(() => {
    if (renderTimer.current) clearTimeout(renderTimer.current);
    renderTimer.current = null;
    if (pendingText.current) {
      const next = textRef.current + pendingText.current; pendingText.current = "";
      textRef.current = next; setText(next); setRawText(next);
      try { localStorage.setItem(TRANSCRIPT_KEY, next); } catch {}
    }
  }, []);
  useEffect(() => { try { localStorage.setItem(TRANSCRIPT_KEY, text); } catch {} }, [text]);

  useEffect(() => { try { localStorage.setItem(NOTE_KEY, notes); } catch {} }, [notes]);

  useEffect(() => {
    if (!active) return;
    alive.current = true;
    api<any>("/settings").then(s => { setHasKey(!!s.has_key); setModel(s.realtime_model || REALTIME_MODEL_ID); }).catch(e => setError(e.message));
    api<any>("/diarization").then(s => setSpeakerReady(!!s.ready)).catch(() => setSpeakerReady(false));
  }, [active]);

  const sc = useCallback((s: string) => { statusRef.current = s; setStatus(s); onStateChange?.(s); }, [onStateChange]);
  const stopMic = useCallback(() => {
    stream.current?.getTracks().forEach(t => t.stop()); stream.current = null;
    proc.current?.disconnect(); proc.current = null;
    ctx.current?.close().catch(() => {}); ctx.current = null;
    setMicLevel(0);
  }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; flushText(); stopMic(); ws.current?.close(); abortCtl.current?.abort(); }; }, [stopMic, flushText]);
  useEffect(() => { if (status !== "live") return; const t = setInterval(() => setElapsed(v => v + 1), 1000); return () => clearInterval(t); }, [status]);

  const start = useCallback(async () => {
    flushText(); pendingText.current = "";
    setSpeakerText(""); setSpeakerDocId(""); setShowSpeakers(false); setSpeakerJobId(""); speakerJob.current = "";
    setError(""); setReviewed(""); setWarnings([]); setSavedPath(""); setRawText(textRef.current); setShowRaw(true); setElapsed(0); sc("connecting");
    try {
      const settings = await api<any>("/settings");
      const cat = await api<string[]>("/provider/models");
      const sel = selectRealtimeModel(cat, settings.realtime_model || "");
      if (!sel) throw new Error("No realtime model available. Open Settings and select a model.");
      if (sel !== settings.realtime_model) await api("/settings", { method: "PUT", body: JSON.stringify({ realtime_model: sel }) });
      setModel(sel);
      const conn = await getConnection();
      const sock = new WebSocket(conn.url.replace(/^http/, "ws") + "/live-transcription");
      ws.current = sock;
      await new Promise<void>((resolve, reject) => {
        const tout = setTimeout(() => reject(new Error("Connection timed out")), 25000);
        sock.onopen = () => sock.send(JSON.stringify({ token: conn.token, title, kind, language: lang, glossary: glossary || undefined, diarization: detectSpeakers, num_speakers: numSpeakers, initial_text: textRef.current }));
        sock.onerror = () => { clearTimeout(tout); reject(new Error("Cannot reach the transcription service")); };
        sock.onmessage = event => {
          const m = JSON.parse(event.data);
          if (m.type === "ready") { clearTimeout(tout); setModel(m.model || sel); resolve(); }
          if (m.type === "delta") { pendingText.current += m.delta; if (!renderTimer.current) renderTimer.current = setTimeout(flushText, 100); }
          if (m.type === "done" || m.type === "saved") {
            flushText();
            if (m.diarization_job_id) { speakerJob.current = m.diarization_job_id; setSpeakerJobId(m.diarization_job_id); }
            if (m.text !== undefined) { textRef.current = m.text; setText(m.text); setRawText(m.text); }
            if (m.document_id) { finalId.current = m.document_id; setDocId(m.document_id); onSaved(m.document_id); }
            finish.current(m.document_id || "");
          }
          if (m.type === "error") { flushText(); clearTimeout(tout); setError(m.message); reject(new Error(m.message)); stopMic(); sc("idle"); }
        };
        sock.onclose = () => { flushText(); clearTimeout(tout); stopMic(); finish.current(finalId.current); if (alive.current && !["summary", "diarization", "finishing"].includes(statusRef.current)) sc("idle"); reject(new Error("Connection closed.")); };
      });
      if (sock.readyState !== WebSocket.OPEN) throw new Error("Session rejected. Check Settings.");
      stream.current = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
      if (sock.readyState !== WebSocket.OPEN) throw new Error("Session closed while microphone permission was pending");
      ctx.current = new AudioContext({ sampleRate: 16000 });
      if (ctx.current.sampleRate !== 16000) throw new Error("Microphone cannot supply 16 kHz");
      await ctx.current.audioWorklet.addModule(workletUrl);
      await ctx.current.resume();
      if (sock.readyState !== WebSocket.OPEN) throw new Error("Session closed before mic streaming");
      proc.current = new AudioWorkletNode(ctx.current, "microphone-pcm");
      proc.current.port.onmessage = event => {
        if (event.data.type === "flushed") flushed.current();
        if (event.data.type === "audio" && sock.readyState === WebSocket.OPEN) {
          if (Number.isFinite(event.data.level)) setMicLevel(event.data.level);
          if (sock.bufferedAmount > 1048576) { setError("Connection too slow."); stopMic(); sock.send(JSON.stringify({ type: "stop" })); return; }
          sock.send(event.data.buffer);
        }
      };
      const src = ctx.current.createMediaStreamSource(stream.current);
      const highpass = ctx.current.createBiquadFilter(); highpass.type = "highpass"; highpass.frequency.value = 80;
      const lowpass = ctx.current.createBiquadFilter(); lowpass.type = "lowpass"; lowpass.frequency.value = 6000;
      const dry = ctx.current.createGain(); dry.gain.value = 0.5;
      const filtered = ctx.current.createGain(); filtered.gain.value = 0.5;
      const sil = ctx.current.createGain(); sil.gain.value = 0;
      src.connect(highpass);
      highpass.connect(dry); dry.connect(proc.current);
      highpass.connect(lowpass); lowpass.connect(filtered); filtered.connect(proc.current);
      proc.current.connect(sil); sil.connect(ctx.current.destination);
      sc("live");
    } catch (e) { flushText(); stopMic(); ws.current?.close(); sc("idle"); setError(e instanceof Error ? e.message : "Microphone access denied"); }
  }, [active, title, kind, lang, glossary, detectSpeakers, numSpeakers, sc, stopMic, flushText]);

  const pollCleanup = useCallback(async (jid: string) => {
    while (alive.current) {
      try {
        const job = await api<any>(`/jobs/${jid}`); onJob(job);
        if (job.status === "succeeded" && job.result) { setReviewed(job.result.answer || ""); setWarnings(job.result.warnings || []); setShowRaw(false); return; }
        if (job.status === "failed") { setError(job.error || "Cleanup failed"); return; }
        if (job.result?.answer) setReviewed(job.result.answer);
      } catch { break; }
      await new Promise(r => setTimeout(r, 750));
    }
  }, [onJob]);

  const waitLocalJob = useCallback(async (id: string) => {
    while (alive.current) {
      const job = await api<any>(`/jobs/${id}`); onJob(job);
      if (job.result?.stage) setSetupStage(job.result.stage);
      if (job.status === "failed") throw new Error(job.error || "Local speaker processing failed");
      if (job.status === "succeeded") return job.result;
      await new Promise(r => setTimeout(r, 750));
    }
    throw new Error("Application closed");
  }, [onJob]);

  const setupSpeakers = useCallback(async () => {
    sc("setup"); setError(""); setSetupStage("Downloading local models…");
    try {
      const ack = await api<any>("/diarization/setup", { method: "POST" });
      const result = await waitLocalJob(ack.id); setSpeakerReady(!!result.ready);
    } catch (e) { setError(e instanceof Error ? e.message : "Model download failed; retry setup."); }
    finally { setSetupStage(""); sc("idle"); }
  }, [sc, waitLocalJob]);

  const speakerResult = useCallback(async (id: string) => {
    sc("diarization");
    const result = await waitLocalJob(id);
    setSpeakerText(result.text); setSpeakerDocId(result.document_id); setShowSpeakers(true);
    setWarnings(result.warnings || []); onSaved(result.document_id);
    return result.document_id as string;
  }, [sc, waitLocalJob, onSaved]);

  const cleanupTranscript = useCallback(async (rawId: string) => {
    if (!rawId) { setError("No transcript to clean up."); return; }
    sc("summary"); setError(""); abortCtl.current?.abort(); abortCtl.current = new AbortController();
    try {
      const ack = await api<any>("/chat", { method: "POST", body: JSON.stringify({ document_ids: [rawId], mode: "transcript_cleanup", language: lang, glossary, question: "Review recognition errors, preserving uncertain terms and the original source." }) });
      onJob(ack); await pollCleanup(ack.id);
    } catch (e) { setError(e instanceof Error ? e.message : "Cleanup failed"); }
    finally { sc("idle"); }
  }, [lang, glossary, onJob, pollCleanup, sc]);

  const pause = useCallback(async () => {
    try {
      if (statusRef.current === "paused") {
        stream.current?.getTracks().forEach(track => { track.enabled = true; });
        await ctx.current?.resume();
        ws.current?.send(JSON.stringify({ type: "resume" })); sc("live");
      } else {
        await ctx.current?.suspend();
        stream.current?.getTracks().forEach(track => { track.enabled = false; });
        flushText(); ws.current?.send(JSON.stringify({ type: "pause" })); sc("paused");
      }
    } catch (e) { setError(e instanceof Error ? e.message : "Cannot pause microphone"); }
  }, [sc, flushText]);

  const stop = useCallback(async () => {
    sc("finishing");
    const finalized = new Promise<string>(resolve => { finish.current = resolve; });
    if (proc.current) await new Promise<void>(resolve => { const t = setTimeout(resolve, 1000); flushed.current = () => { clearTimeout(t); resolve(); }; proc.current!.port.postMessage("flush"); });
    flushText();
    stopMic();
    if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify({ type: "stop" }));
    else finish.current(finalId.current);
    await Promise.race([finalized, new Promise<string>(r => setTimeout(() => r(finalId.current), 35000))]);
    try {
      if (speakerJob.current) await speakerResult(speakerJob.current);
    } catch (e) { setError(e instanceof Error ? e.message : "Speaker processing failed; raw transcript retained."); }
    finally { setSetupStage(""); sc("idle"); }
  }, [stopMic, speakerResult, sc, flushText]);

  const retrySpeakers = useCallback(async () => {
    setError("");
    try {
      const current = await api<any>(`/jobs/${speakerJobId}`);
      if (current.status === "failed") await api(`/jobs/${speakerJobId}/retry`, { method: "POST" });
      await speakerResult(speakerJobId);
    } catch (e) { setError(e instanceof Error ? e.message : "Retry failed"); }
    finally { setSetupStage(""); sc("idle"); }
  }, [speakerJobId, speakerResult, sc]);

  const nativeSave = useCallback(async (fname: string, content: string) => {
    if (!window.mynotes?.saveTextFile) throw new Error("The native Save as dialog is unavailable. Reopen the updated desktop app.");
    const result = await window.mynotes.saveTextFile({ name: fname.replace(/\.md$/i, ""), text: content, format: "md" });
    if (result) setSavedPath(result.path);
    return result;
  }, []);

  const displayText = showSpeakers && speakerText ? speakerText : reviewed && !showRaw ? reviewed : text;
  const saveTranscriptAs = useCallback(async () => { if (!text.trim()) return; try { await nativeSave((title || "transcript") + ".md", displayText); } catch (e) { setError(e instanceof Error ? e.message : "Save failed"); } }, [text, displayText, title, nativeSave]);
  const saveNotesAs = useCallback(async () => { if (!notes.trim()) return; try { await nativeSave((title || "personal-notes") + "-notes.md", notes); } catch (e) { setError(e instanceof Error ? e.message : "Save failed"); } }, [notes, title, nativeSave]);
  const saveSessionAs = useCallback(async () => { if (!text.trim() && !notes.trim()) return; try { await nativeSave((title || "session") + ".md", [
    `# Transcript\n\n${speakerText || text || "(empty)"}`, `\n# Personal Notes\n\n${notes || "(empty)"}`
  ].filter(Boolean).join("\n---\n")); } catch (e) { setError(e instanceof Error ? e.message : "Save failed"); } }, [text, speakerText, notes, title, nativeSave]);

  const deleteTranscript = useCallback(async () => {
    if (!docId || status !== "idle") return;
    try { await api(`/documents/${docId}`, { method: "DELETE" }); if (speakerDocId) await api(`/documents/${speakerDocId}`, { method: "DELETE" }); setDocId(""); setRawText(""); setText(""); setReviewed(""); setShowRaw(false); setSpeakerText(""); setSpeakerDocId(""); setShowSpeakers(false); setSpeakerJobId(""); speakerJob.current = ""; onSaved(""); }
    catch (e) { setError(e instanceof Error ? e.message : "Delete failed"); }
  }, [docId, speakerDocId, status, onSaved]);

  const live = status === "live";
  const capturing = live || status === "paused";
  const hasText = !!(text || rawText);

  return <section className="meeting-page">
    <div className="page-eyebrow">LIVE NOTES <span className="muted">· Voice to knowledge</span></div>
    <input className="page-title-input" aria-label="Session title" value={title} onChange={e => setTitle(e.target.value)} disabled={live} />
    <p className="page-description">Speak naturally. Follow the transcript live and take personal notes.</p>
    <div className="meeting-toolbar">
      <label><input type="checkbox" checked={detectSpeakers} onChange={e => setDetectSpeakers(e.target.checked)} disabled={!speakerReady || status !== "idle"} /> Distinguish speakers (local)</label>
      {!speakerReady && <button onClick={setupSpeakers} disabled={status !== "idle"}>Download speaker models (~120 MB)</button>}
      {detectSpeakers && <label>Speakers <select aria-label="Number of speakers" value={numSpeakers} onChange={e => setNumSpeakers(Number(e.target.value))} disabled={status !== "idle"}><option value={0}>Auto</option>{Array.from({ length: 9 }, (_, i) => i + 2).map(n => <option key={n} value={n}>{n}</option>)}</select></label>}
      <small className="muted">Free CPU processing after Stop · temporary local audio · estimated speaker labels</small>
    </div>
    <div className="meeting-toolbar">
      <label className="setting-select">Note kind
        <select value={kind} onChange={e => setKind(e.target.value as NoteKind)} disabled={live}><option value="lecture">Lecture</option><option value="meeting">Meeting</option></select>
      </label>
      <label className="setting-select">Language
        <select value={lang} onChange={e => setLang(e.target.value as Lang)} disabled={live}><option value="auto">Auto-detect</option><option value="fr">Français</option><option value="en">English</option></select>
        <small className="muted">(guides AI, not Voxtral ASR)</small>
      </label>
      <label className="setting-select" style={{ flex: 1 }}>Glossary (optional)
        <input value={glossary} onChange={e => setGlossary(e.target.value)} disabled={live} placeholder="e.g. Alice, Bob, Project Nova" />
      </label>
    </div>
    <div className="meeting-toolbar">
      {status === "idle" ? <button className="primary-button" onClick={hasKey ? start : onSettings}>{hasKey ? text.trim() ? "● Restart live transcription" : "● Start live transcription" : "Connect Cloud Temple →"}</button>
        : <button className="primary-button" disabled={!capturing} onClick={() => stop()}>{capturing ? "■ Stop recording" : status === "connecting" ? "Connecting to Voxtral…" : status === "diarization" ? "Distinguishing speakers…" : status === "setup" ? "Downloading models…" : "Finishing transcript…"}</button>}
      <button disabled={!capturing} onClick={pause}>{status === "paused" ? "Resume audio" : "Pause audio"}</button>
      <span className={`status-pill ${live ? "is-live" : ""}`}>{live ? "Listening" : status === "paused" ? "Paused" : status === "idle" ? "Ready" : "Working"}</span>
      <span className="muted">{formatTime(elapsed)} · {model}</span>
      <button className="text-button" onClick={onSettings}>Settings</button>
    </div>
    <div className="meeting-toolbar">
      <span>Microphone signal</span>
      <meter aria-label="Microphone signal" min={0} max={1} value={Math.min(1, micLevel * 5)} />
      {live && <small>{micLevel >= 0.7 ? "Input too loud — lower microphone level in Settings" : micLevel < 0.003 ? "Very quiet input — check the physical mic switch or microphone level in Settings" : "Audio signal detected"}</small>}
    </div>
    {error && <div className="error-banner" role="alert">{error}</div>}
    {setupStage && <p role="status">{setupStage}</p>}
    {speakerJobId && !speakerText && status === "idle" && <div className="button-row"><button onClick={retrySpeakers}>Retry speaker detection</button></div>}
    {warnings.map((warning, i) => <div className="error-banner" key={i}>{warning}</div>)}
    {savedPath && <p role="status" className="success-message">Saved to {savedPath}</p>}
    {!hasKey && <div className="setup-banner"><span>Add your API key for live transcription.</span><button onClick={onSettings}>Set up AI</button></div>}
    <div className="meeting-columns">
      {/* Panel 1: Raw live transcript */}
      <article className="paper-panel">
        <header><span>Transcript</span><small>{live ? "Appearing as you speak" : rawText ? "Live raw + final" : docId ? "Saved" : "Paste or start live"}</small></header>
        {(reviewed || speakerText) && <div className="button-row"><button onClick={() => { setShowRaw(true); setShowSpeakers(false); }}>Original transcript</button>{speakerText && <button onClick={() => setShowSpeakers(true)}>Speaker-labelled transcript</button>}{reviewed && <button onClick={() => { setShowRaw(false); setShowSpeakers(false); }}>Reviewed draft</button>}</div>}
        <textarea className="transcript-editor" aria-label="Live transcript" value={capturing ? text : displayText} readOnly={capturing || showSpeakers} onChange={e => reviewed && !showRaw ? setReviewed(e.target.value) : setText(e.target.value)} placeholder="Your words appear here during the session. You can also paste an existing transcript." />
        <footer className="panel-actions">
          <button onClick={saveTranscriptAs} disabled={!text.trim()}>Save transcript as…</button>
          <button onClick={deleteTranscript} disabled={!docId || status !== "idle"} title="Delete from library">Delete from library</button>
          {hasText && docId && <button disabled={!hasKey || status !== "idle"} onClick={() => cleanupTranscript(docId)}>Review recognition errors (AI)</button>}
        </footer>
      </article>
      {/* Panel 2: Personal notes (always editable, localStorage) */}
      <article className="paper-panel">
        <header><span>Personal notes · Markdown</span><small>Draft saved locally</small></header>
        <MarkdownEditor value={notes} onChange={setNotes} label="Personal notes" />
        <footer className="panel-actions"><button onClick={saveNotesAs} disabled={!notes.trim()}>Save notes as…</button></footer>
      </article>
    </div>
    <div className="meeting-toolbar" style={{ justifyContent: "center", marginTop: 12 }}>
      <button className="primary-button" onClick={saveSessionAs} disabled={!text.trim() && !notes.trim()}>Save session as…</button>
    </div>
  </section>;
}
