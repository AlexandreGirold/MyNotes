import { useRef, useState } from "react";
import TextDocument from "./TextDocument";

export default function MarkdownEditor({ value, onChange, label, placeholder }: {
  value: string; onChange: (value: string) => void; label: string; placeholder?: string;
}) {
  const [preview, setPreview] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);
  const format = (prefix: string, suffix: string, fallback: string) => {
    if (!editor.current) return;
    const target = editor.current, start = target.selectionStart, end = target.selectionEnd;
    const selected = value.slice(start, end) || fallback;
    const lead = prefix.endsWith(" ") && start > 0 && value[start - 1] !== "\n" ? "\n" : "";
    onChange(value.slice(0, start) + lead + prefix + selected + suffix + value.slice(end));
    setTimeout(() => { target.focus(); target.setSelectionRange(start + lead.length + prefix.length, start + lead.length + prefix.length + selected.length); }, 0);
  };
  // ponytail: bounded preview avoids thousands of DOM nodes; the full source remains editable/savable.
  const shown = preview ? value.slice(0, 50000).split("\n").slice(0, 500).join("\n") : "";
  return <div className="markdown-input">
    <div className="markdown-toolbar" aria-label="Markdown formatting">
      <button type="button" disabled={preview} onClick={() => format("## ", "", "Heading")}>Heading</button>
      <button type="button" disabled={preview} onClick={() => format("**", "**", "bold text")}>Bold</button>
      <button type="button" disabled={preview} onClick={() => format("- ", "", "Item")}>List</button>
      <button type="button" disabled={preview} onClick={() => format("- [ ] ", "", "Task")}>Task</button>
      <button type="button" aria-pressed={preview} onClick={() => setPreview(state => !state)}>{preview ? "Edit Markdown" : "Preview Markdown"}</button>
    </div>
    {preview ? <div className="personal-notes-preview" aria-label={`${label} preview`}>
      {shown.length < value.length && <p className="field-hint">Preview limited to 500 lines / 50,000 characters. Your full note is preserved.</p>}
      <TextDocument text={shown} />
    </div> : <textarea ref={editor} className="personal-notes-editor transcript-editor markdown-editor" aria-label={label} value={value} onChange={event => onChange(event.target.value)} onKeyDown={event => {
      if (event.key !== "Tab" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
      event.preventDefault();
      const target = event.currentTarget, start = target.selectionStart, end = target.selectionEnd;
      onChange(value.slice(0, start) + "\t" + value.slice(end));
      setTimeout(() => target.setSelectionRange(start + 1, start + 1), 0);
    }} placeholder={placeholder || "## Key ideas\n\n- My observations\n- [ ] Follow up\n\nUse **bold** or `code`."} />}
  </div>;
}
