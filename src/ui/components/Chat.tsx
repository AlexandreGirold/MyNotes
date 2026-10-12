import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatMessage, Job, SourceCallback } from "../types";
import TextDocument from "./TextDocument";

type Props = { selectedIds: string[]; onJob: (job: any) => void; onSource: SourceCallback; onSettings: () => void };
type Conversation = { id: string; title: string };
export default function Chat({ selectedIds, onJob, onSource, onSettings }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("global");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  const [model, setModel] = useState("gpt-oss:120b");
  const [fileModel, setFileModel] = useState("qwen3.6:27b");
  const [harness, setHarness] = useState(true);
  const [agentAvailable, setAgentAvailable] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState("");
  const [steps, setSteps] = useState<{ tool: string; status: string; title: string }[]>([]);
  const alive = useRef(true);
  const scroll = useRef<HTMLDivElement>(null);
  const busy = !!status;
  const openConversation = async (id: string) => {
    setStatus("Loading conversation…"); setError(""); setSteps([]);
    try {
      const data = await api<ChatMessage[]>(`/chats?conversation_id=${encodeURIComponent(id)}`);
      if (!alive.current) return;
      setMessages(data); setConversationId(id); setQuestion("");
      localStorage.setItem("mynotes-active-conversation", id);
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "Could not load conversation"); }
    finally { if (alive.current) setStatus(""); }
  };
  const newConversation = async () => {
    if (busy) return;
    setStatus("Creating conversation…"); setError("");
    try {
      const created = await api<Conversation>("/conversations", { method: "POST" });
      if (!alive.current) return;
      setConversations(previous => [created, ...previous]);
      await openConversation(created.id);
    } catch (e) { if (alive.current) { setError(e instanceof Error ? e.message : "Could not create conversation"); setStatus(""); } }
  };
  const deleteConversation = async (id: string) => {
    if (busy) return;
    setStatus("Deleting conversation…"); setError("");
    try {
      await api(`/conversations/${id}`, { method: "DELETE" });
      if (!alive.current) return;
      const remaining = conversations.filter(c => c.id !== id);
      setConversations(remaining);
      if (id === conversationId) {
        if (remaining.length) await openConversation(remaining[0].id);
        else { setConversationId(""); setMessages([]); setQuestion(""); setSteps([]); localStorage.removeItem("mynotes-active-conversation"); }
      }
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : "Could not delete conversation"); }
    finally { if (alive.current) setStatus(""); }
  };
  useEffect(() => {
    alive.current = true;
    setStatus("Loading conversations…");
    api<Conversation[]>("/conversations").then(async data => {
      if (!alive.current) return;
      setConversations(data);
      const saved = localStorage.getItem("mynotes-active-conversation");
      const current = data.find(c => c.id === saved) || data[0];
      if (current) await openConversation(current.id); else setStatus("");
    }).catch(e => { if (alive.current) { setError(e.message); setStatus(""); } });
    api<any>("/settings").then(data => { if (alive.current) { setHasKey(data.has_key); setModel(data.global_chat_model || "gpt-oss:120b"); setFileModel(data.chat_model || "qwen3.6:27b"); setHarness(data.chat_harness !== false); setAgentAvailable(!!data.agent_available); } }).catch(e => setError(e.message));
    return () => { alive.current = false; };
  }, []);
  useEffect(() => { scroll.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, status]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!question.trim() || busy) return;
    if (!harness && mode === "documents" && !selectedIds.length) { setError("Select files in Notes & files, then ask your question here."); return; }
    if (harness && !agentAvailable && !["web", "files"].includes(mode)) { setError("OpenCode is unavailable. Install it or switch to direct chat."); return; }
    const prompt = question.trim(); setError(""); setStatus("Sending your question…");
    try {
      let id = conversationId;
      if (!id) {
        const created = await api<Conversation>("/conversations", { method: "POST" });
        id = created.id; setConversationId(id); setConversations(previous => [created, ...previous]);
        localStorage.setItem("mynotes-active-conversation", id);
      }
      const ack = await api<any>("/chat", { method: "POST", body: JSON.stringify({ question: prompt,
        mode, harness: ["web", "files"].includes(mode) ? false : harness, conversation_id: id, document_ids: ["web", "files"].includes(mode) ? [] : harness || mode !== "global" ? selectedIds : [] }) });
      onJob(ack);
      setQuestion("");
      setMessages(previous => [...previous, { id: "pending", question: prompt, answer: "", citations: [], warnings: [], mode, created_at: Date.now() / 1000 }]);
      for (let i = 0; i < 360; i++) {
        const job = await api<Job>(`/jobs/${ack.id}`); onJob(job);
        if (!alive.current) return;
        if (job.status === "failed") throw new Error(job.error || "The model could not answer");
        if (job.result?.steps) setSteps(job.result.steps as any);
        if (job.result?.answer) setMessages(previous => previous.map(message => message.id === "pending" ? { ...message, answer: String(job.result?.answer) } : message));
        if (job.status === "succeeded") {
          setMessages(previous => previous.map(message => message.id === "pending" ? { ...message, id: job.id, answer: String(job.result?.answer || ""), citations: (job.result?.citations || []) as any, warnings: (job.result?.warnings || []) as string[] } : message));
          setConversations(previous => previous.map(c => c.id === id && c.title === "New conversation" ? { ...c, title: prompt.slice(0, 80) } : c));
          return;
        }
        setStatus(job.status === "queued" ? "Waiting for the assistant…" : mode === "files" ? "Searching and reading your allowed laptop files…" : mode === "web" ? "Reading public Internet sources…" : harness ? "OpenCode is inspecting sources and working through your request…" : "Thinking with your selected model…");
        await new Promise(resolve => setTimeout(resolve, 800));
      }
      throw new Error("The request is still running. You can check it in the workspace.");
    } catch (e) { if (alive.current) { setError(e instanceof Error ? e.message : "Could not send the question"); setQuestion(prompt); setMessages(previous => previous.filter(message => message.id !== "pending")); } }
    finally { if (alive.current) setStatus(""); }
  };

  return <section className="assistant-page">
    <div className="page-eyebrow">YOUR WORKSPACE ASSISTANT</div>
    <div className="page-heading"><h1>Ask, explore, understand.</h1></div>
    <div className="assistant-layout">
    <aside className="conversation-sidebar"><h2>Discussions</h2><button className="primary-button" disabled={busy} onClick={newConversation}>New conversation</button>
      <nav className="conversation-list" aria-label="Conversations">{conversations.map((c, i) => <div className="conversation-row" key={c.id}><button className="conversation-open" disabled={busy} data-conversation-id={c.id} aria-pressed={c.id === conversationId} onClick={() => openConversation(c.id)} title={c.title}>{c.title === "New conversation" ? `New conversation · ${conversations.length - i}` : c.title}</button><button className="conversation-delete" disabled={busy} aria-label={`Delete conversation: ${c.title}`} title="Delete conversation" onClick={() => deleteConversation(c.id)}>×</button></div>)}</nav>
      {!conversations.length && <p className="field-hint">Create a discussion to keep its messages separate.</p>}
    </aside>
    <div className="assistant-discussion">
    <p className="page-description">Have a conversation, ask about your files, or turn a long document into useful notes.</p>
    <div className="assistant-toolbar">
      <select aria-label="Chat task" value={mode} onChange={e => setMode(e.target.value)} disabled={busy}>
        <option value="global">General conversation</option><option value="documents">Ask selected files</option><option value="files">Search laptop files</option><option value="web">Search the web</option>
      </select>
      <span className="muted">{mode === "files" ? fileModel : mode !== "documents" ? model : `${selectedIds.length} source${selectedIds.length === 1 ? "" : "s"} selected`}</span>
      <label className="check-label"><input type="checkbox" checked={harness} disabled={busy} onChange={e => setHarness(e.target.checked)} />Agent harness</label>
      <span className="muted">{mode === "files" ? "Search & read Settings’ allowed folders before answering" : mode === "web" ? "Search & read public sources before answering" : harness && agentAvailable ? "Allowed-file & public web tools enabled" : "Choose Search laptop files or Search the web for research"}</span>
      <button className="text-button" onClick={onSettings}>Configure AI</button>
    </div>
    {hasKey === false && <div className="setup-banner"><span>Connect your Cloud Temple account to use the assistant.</span><button onClick={onSettings}>Add API key →</button></div>}
    {steps.length > 0 && <details className="agent-steps" open={busy}><summary>Agent steps · {steps.length}</summary>{steps.map((step, i) => <p key={i}>{step.status === "completed" ? "✓" : "◌"} {step.title || step.tool} <small>{step.tool}</small></p>)}</details>}
    <div className="conversation" role="log" aria-label="Conversation" aria-live="polite">
      {!messages.length && <div className="assistant-empty"><span className="empty-symbol">✦</span><h2>A little clarity goes a long way.</h2>
        <p>What would you like to work on?</p><div className="prompt-suggestions">
          {["Help me understand a difficult concept", "Summarize the key ideas in my notes", "Help me prepare questions for my next meeting"].map(prompt => <button key={prompt} onClick={() => setQuestion(prompt)}>{prompt} ↗</button>)}
        </div></div>}
      {messages.map(message => <article className="conversation-turn" key={message.id}>
        <div className="user-message"><span className="avatar">You</span><p>{message.question}</p></div>
        {message.answer && <div className="assistant-message"><span className="avatar ai-avatar">✦</span><div><TextDocument text={message.answer.replace(/\[source:[^\]]+\]/g, "")} />
          {!!message.citations?.length && <div className="source-chips">{message.citations.map((citation, i) => /^https:\/\//i.test(citation.source_id) ? <a key={i} href={citation.source_id} target="_blank" rel="noreferrer">↗ {citation.title || `Source ${i + 1}`}</a> : <button key={i} onClick={() => onSource(citation)}>↗ {citation.title || `Source ${i + 1}`}</button>)}</div>}
          {message.warnings?.map((warning, i) => <p key={i} className="field-hint">{warning}</p>)}
        </div></div>}
      </article>)}
      {status && <div className="thinking"><span className="spinner" />{status}</div>}
      <div ref={scroll} />
    </div>
    {error && <div className="error-banner" role="alert">{error}<button className="text-button" onClick={onSettings}>Open settings</button></div>}
    <form className="chat-composer" onSubmit={submit}>
      <textarea aria-label="Question" value={question} onChange={e => setQuestion(e.target.value)}
        placeholder={mode === "files" ? "Ask about a topic or a file in your allowed folders…" : mode === "web" ? "Search the Internet, or paste a public HTTPS URL…" : mode === "global" ? "Ask anything…" : "Ask about your selected sources…"} rows={2} disabled={busy}
        onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }} />
      <div className="composer-footer"><span>Enter to send · Shift + Enter for a new line</span><button className="primary-button" disabled={!question.trim() || busy || !hasKey}>Send ↑</button></div>
    </form>
    </div></div>
  </section>;
}
