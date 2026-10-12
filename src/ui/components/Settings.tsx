import { useEffect, useRef, useState } from "react";
import { api, downloadFile } from "../api";
import type { SettingsOut } from "../types";
import { CLOUD_TEMPLE_MODELS, REALTIME_MODEL_ID, realtimeModels, selectRealtimeModel } from "../models";

export default function Settings({ onJob }: { onJob: (job: any) => void }) {
  const [form, setForm] = useState<SettingsOut & { api_key?: string }>({});
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [liveModelWarning, setLiveModelWarning] = useState("");
  const [microphone, setMicrophone] = useState<{ volume: number; muted: boolean } | null>(null);
  const [micBusy, setMicBusy] = useState(false);
  const [micError, setMicError] = useState("");
  const [micMessage, setMicMessage] = useState("");
  const _userChanged = useRef(false);
  const _micInFlight = useRef(false);
  useEffect(() => {
    if (!window.mynotes?.getMicrophoneVolume) { setMicError("Reopen the updated desktop app to control the microphone."); return; }
    window.mynotes.getMicrophoneVolume().then(v => {
      if (_userChanged.current) return;
      setMicrophone(v);
    }).catch(e => setMicError(e.message));
  }, []);
  const applyMicrophone = async (volume: number) => {
    if (_micInFlight.current) return;
    _micInFlight.current = true;
    setMicBusy(true); setMicError(""); setMicMessage("");
    try { const current = await window.mynotes.setMicrophoneVolume(volume); if (current.volume !== volume) throw new Error(`Microphone level changed externally: requested ${volume}%, actual ${current.volume}%.`); setMicrophone(current); setMicMessage(`Microphone input set to ${current.volume}%.`); }
    catch (e) { setMicError(e instanceof Error ? e.message : "Could not adjust microphone volume."); try { setMicrophone(await window.mynotes.getMicrophoneVolume()); } catch { /* Keep the error visible if readback is unavailable. */ } }
    finally { _micInFlight.current = false; setMicBusy(false); }
  };
  const loadModels = async (settings: SettingsOut) => {
    const catalogue = await api<string[]>("/provider/models");
    setModels(catalogue);
    const selected = selectRealtimeModel(catalogue, settings.realtime_model || "");
    if (selected) {
      if (selected !== settings.realtime_model) {
        await api("/settings", { method: "PUT", body: JSON.stringify({ realtime_model: selected }) });
        setForm(previous => ({ ...previous, realtime_model: selected }));
      }
      setLiveModelWarning("");
    } else {
      setLiveModelWarning("No matching realtime model was found in your account catalogue. Chat can still work; live transcription requires a realtime model enabled by Cloud Temple. Select its exact ID if listed, or confirm access with Cloud Temple.");
    }
    return catalogue;
  };
  useEffect(() => { api<SettingsOut>("/settings").then(async settings => {
    setForm(settings);
    if (settings.has_key) await loadModels(settings);
  }).catch(e => setError(e.message)); }, []);
  const change = (field: string, value: string | number | boolean) => setForm(previous => ({ ...previous, [field]: value }));
  const connect = async () => {
    setBusy(true); setMessage(""); setError("");
    try {
      const knownCloudTemple = form.base_url?.replace(/\/$/, "") === "https://api.ai.cloud-temple.com/v1";
      const { has_key: _hasKey, ...settings } = form;
      const saved = await api<SettingsOut>("/settings", { method: "PUT", body: JSON.stringify({ ...settings,
        api_key: form.api_key || undefined, verified_chat: knownCloudTemple || form.verified_chat,
        verified_realtime: knownCloudTemple || form.verified_realtime,
      }) });
      setForm(previous => ({ ...previous, ...saved, api_key: "" }));
      const catalogue = await loadModels(saved);
      setMessage(`Connected to Cloud Temple · ${catalogue.length} models available. Voxtral uses the realtime engine ID documented for /v1/realtime.`);
    } catch (e) { setError(e instanceof Error ? e.message : "Connection failed"); }
    finally { setBusy(false); }
  };
  const waitJob = async (ack: any) => {
    for (let i = 0; i < 180; i++) {
      const job = await api<any>(`/jobs/${ack.id}`); onJob(job);
      if (job.status === "failed") throw new Error(job.error);
      if (job.status === "succeeded") return job.result;
      await new Promise(resolve => setTimeout(resolve, 750));
    }
    throw new Error("This operation is still processing");
  };
  const modelFields = [
    ["global_chat_model", "General conversations", "gpt-oss:120b"],
    ["chat_model", "Questions about files", "qwen3.6:27b"],
    ["audio_summary_model", "Meeting summaries", "gpt-oss:120b"],
    ["document_summary_model", "Long-document summaries", "qwen3.6:27b"],
    ["realtime_model", "Live transcription", REALTIME_MODEL_ID],
    ["vision_model", "Images and diagrams", "qwen3-vl:30b"],
  ] as const;
  const ranked = CLOUD_TEMPLE_MODELS.filter(m => models.includes(m[0]) && m[2] > 0).sort((a, b) => b[2] - a[2]);
  const optionsFor = (field: string) => field === "realtime_model" ? [...new Set([REALTIME_MODEL_ID, ...realtimeModels(models)])] : [...new Set([
    ...CLOUD_TEMPLE_MODELS.filter(m => field === "vision_model" ? m[1] === "Vision" || ["qwen3.6:27b", "qwen3.6:35b", "mistral-small4:119b", "devstral-small-2:24b"].includes(m[0]) : ["Chat", "Code", "Vision", "Translation"].includes(m[1])).map(m => m[0]),
    ...models.filter(id => !CLOUD_TEMPLE_MODELS.some(m => m[0] === id) && !/realtime|voxtral|embedding|rerank/i.test(id)),
  ])];
  return <section className="settings-page">
    <div className="page-eyebrow">MAKE THE WORKSPACE YOURS</div><h1>Settings & connections</h1>
    <p className="page-description">One Cloud Temple connection. The right model for each task.</p>
    <article className="settings-card"><h2>Microphone</h2>
      <label htmlFor="microphone-volume">Microphone sensitivity · {microphone ? `${microphone.volume}%` : "Unavailable"}
        <input id="microphone-volume" type="range" min={0} max={120} step={1} value={microphone?.volume ?? 0} disabled={!microphone || micBusy}
          aria-valuetext={microphone ? `${microphone.volume}%` : "Unavailable"}
          onChange={e => { _userChanged.current = true; setMicrophone(previous => previous && ({ ...previous, volume: Number(e.target.value) })); setMicMessage(""); }}
          onPointerUp={e => applyMicrophone(Number(e.currentTarget.value))}
          onKeyUp={e => { if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) applyMicrophone(Number(e.currentTarget.value)); }} />
      </label>
      <div className="button-row"><button disabled={!microphone || micBusy} onClick={() => applyMicrophone(microphone!.volume)}>{micBusy ? "Applying…" : "Apply microphone level"}</button>
        <button disabled={micBusy} onClick={async () => { try { setMicrophone(await window.mynotes.getMicrophoneVolume()); setMicMessage(""); setMicError(""); } catch (e) { setMicError(e instanceof Error ? e.message : "Could not read microphone."); } }}>Read actual level</button></div>
      <p className="field-hint">Release the slider or use the arrow keys to apply immediately. Controls the system default microphone, including other applications. Above 100% adds software amplification.</p>
      {microphone?.muted && <p className="field-hint">The microphone is muted. Unmute it in your system audio settings.</p>}
      {micMessage && <p role="status" className="success-message">{micMessage}</p>}
      {micError && <div role="alert" className="error-banner">{micError}</div>}
    </article>
    <article className="settings-card"><div className="settings-card-heading"><div><h2>Cloud Temple</h2><p>Connect your sovereign AI workspace.</p></div><span className="status-pill">{form.has_key ? form.credential_storage === "encrypted" ? "Saved securely" : "This session only" : "Not connected"}</span></div>
      <label>API key<input type="password" autoComplete="off" aria-label="Cloud Temple API key" value={form.api_key || ""} placeholder={form.has_key ? "A key is saved · enter a new one to replace it" : "Paste your Cloud Temple LLMaaS key"} onChange={e => change("api_key", e.target.value)} /></label>
      <p className="field-hint">{form.has_key && form.credential_storage === "encrypted" ? "Your saved key is loaded. This field stays blank on purpose; you do not need to enter it again. Enter a new key only to replace it." : form.credential_storage === "session" ? "Secure system storage is unavailable. The key works for this session only; enable your OS keyring to keep it across restarts." : "Your key is used by the backend and saved with encrypted system storage when available."}</p>
      {form.credential_error && <div className="error-banner" role="alert">{form.credential_error}</div>}
      <button className="primary-button" onClick={connect} disabled={busy}>{busy ? "Checking connection…" : "Save & connect"}</button>
      {message && <p className="success-message" role="status">{message}</p>}
      {error && <div className="error-banner" role="alert">{error}</div>}
    </article>
    <article className="settings-card"><h2>Models by task</h2><p className="muted">Balanced defaults for quality and responsiveness. Type an ID or choose from your account catalogue.</p>
      <div className="model-grid">{modelFields.map(([field, label, fallback]) => <label key={field}>{label}
        <select aria-label={label} value={form[field] || ""} onChange={e => change(field, e.target.value)}>
          <option value="">Choose a model</option>
          {[...new Set([...(form[field] ? [form[field]!] : []), ...optionsFor(field)])].map(id => { const info = CLOUD_TEMPLE_MODELS.find(m => m[0] === id); const rank = ranked.findIndex(m => m[0] === id); return <option key={id} value={id}>{id}{info?.[2] ? ` · ${info[2]} tokens/s` : ""}{rank >= 0 ? ` · speed #${rank + 1}/${ranked.length}` : ""}{models.includes(id) ? " · available" : ""}</option>; })}
        </select>
        <input aria-label={`${label} custom model ID`} value={form[field] || ""} placeholder={fallback} onChange={e => change(field, e.target.value)} />
      </label>)}</div>
      <button disabled={busy || !form.has_key} onClick={async () => { setBusy(true); setError(""); try { await loadModels(form); } catch (e) { setError(e instanceof Error ? e.message : "Catalogue unavailable"); } finally { setBusy(false); } }}>Refresh account models</button>
      <details><summary>Full Cloud Temple catalogue · {CLOUD_TEMPLE_MODELS.length} models</summary>
        <p className="field-hint">Published throughput targets, not measured here. Speed ranks compare account-available models with published speeds, not answer quality. No comparable quality benchmark is published. Embedding, reranking and image generation are listed for reference.</p>
        <a href="https://www.cloud-temple.com/en/products/large-language-model-as-a-service-llmaas/" target="_blank" rel="noreferrer">Cloud Temple source</a>
        {CLOUD_TEMPLE_MODELS.map(([id, category, speed, context, strengths]) => <p key={id}><strong>{id}</strong> · {category} · {speed ? `${speed} tokens/s` : "speed not published"} · {context ? `${context.toLocaleString()} context tokens` : "context not published"} · {strengths}{models.includes(id) ? " · available in your account" : " · account access unconfirmed"}</p>)}
      </details>
      {liveModelWarning && <p className="field-hint" role="status">{liveModelWarning}</p>}
      <button onClick={connect} disabled={busy}>Save model choices</button>
    </article>
    <article className="settings-card"><h2>Agent harness · allowed directories</h2>
      <p className="muted">OpenCode can list, search and read files only in these folders and the selected MyNotes sources. Symlink escapes and credential files are blocked.</p>
      <label className="check-label"><input type="checkbox" checked={form.chat_harness !== false} onChange={e => change("chat_harness", e.target.checked)} />Use OpenCode agent harness for chat</label>
      <p className="field-hint">{form.agent_available ? "OpenCode is available." : "OpenCode is not installed; install it to enable tool-using chat."}</p>
      {(form.agent_directories || []).map(directory => <div className="button-row" key={directory}><code>{directory}</code><button onClick={() => setForm(previous => ({ ...previous, agent_directories: previous.agent_directories?.filter(value => value !== directory) }))}>Remove</button></div>)}
      <button onClick={async () => {
        try { const selected = await window.mynotes.selectDirectories(); setForm(previous => ({ ...previous, agent_directories: [...new Set([...(previous.agent_directories || []), ...selected])] })); }
        catch (failure) { setError(failure instanceof Error ? failure.message : "Folder picker failed"); }
      }}>Add allowed folders…</button>
      <button disabled={busy} onClick={connect}>Save agent settings</button>
    </article>
    <details className="settings-card"><summary>Advanced connection settings</summary><div className="model-grid">
      {([['base_url', 'Base URL'], ['chat_path', 'Chat path'], ['transcription_path', 'File transcription path'], ['transcription_model', 'File transcription model']] as const).map(([field, label]) => <label key={field}>{label}<input value={form[field] || ""} onChange={e => change(field, e.target.value)} /></label>)}
      <label>Request timeout (seconds)<input type="number" min={1} max={600} value={form.timeout ?? 60} onChange={e => change("timeout", Number(e.target.value))} /></label>
      <label>File question context budget<input type="number" min={100} value={form.context_budget ?? 12000} onChange={e => change("context_budget", Number(e.target.value))} /></label>
      {([['verified_chat', 'Chat endpoint confirmed'], ['verified_realtime', 'Voxtral realtime endpoint confirmed'], ['verified_vision', 'Vision endpoint confirmed']] as const).map(([field, label]) => <label className="check-label" key={field}><input type="checkbox" checked={!!form[field]} onChange={e => change(field, e.target.checked)} />{label}</label>)}
    </div></details>
    <article className="settings-card"><h2>Your data</h2><p className="muted">Back up the local library. Provider credentials are excluded.</p><div className="button-row">
      <button disabled={busy} onClick={async () => { setBusy(true); setError(""); try {
        const result = await waitJob(await api("/backups", { method: "POST" }));
        const url = URL.createObjectURL(await downloadFile(result.backup_path)); const a = document.createElement("a"); a.href = url; a.download = "mynotes-backup.zip"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (e) { setError(e instanceof Error ? e.message : "Backup failed"); } finally { setBusy(false); } }}>Export backup</button>
      <label className="file-button">Restore backup<input type="file" accept=".zip" disabled={busy} onChange={async e => {
        const file = e.target.files?.[0]; if (!file) return; setBusy(true); setError("");
        try { const body = new FormData(); body.append("file", file); await waitJob(await api("/restore", { method: "POST", body })); setMessage("Backup restored. Reopen Notes & files to refresh your library."); }
        catch (failure) { setError(failure instanceof Error ? failure.message : "Restore failed"); } finally { setBusy(false); }
      }} /></label>
    </div></article>
  </section>;
}
