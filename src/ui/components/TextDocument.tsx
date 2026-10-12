import { Fragment } from "react";

function inline(text: string) {
  return text.split(/(\[[^\]]+\]\(https?:\/\/[^\s)]+\)|\*\*[^*]+\*\*|`[^`]+`)/).map((part, i) => {
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
    if (link) return <a key={i} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
    return (
    part.startsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> :
    part.startsWith("`") ? <code key={i}>{part.slice(1, -1)}</code> : <Fragment key={i}>{part}</Fragment>);
  });
}

export default function TextDocument({ text }: { text: string }) {
  return <div className="text-document">{text.split("\n").map((line, i) => {
    const heading = line.match(/^#{1,3}\s+(.+)/);
    if (heading) return <h3 key={i}>{inline(heading[1])}</h3>;
    const task = line.match(/^[-*]\s+\[([ xX])\]\s+(.+)/);
    if (task) return <div className="document-bullet" key={i}><input type="checkbox" checked={task[1] !== " "} readOnly aria-label={task[2]} /><div>{inline(task[2])}</div></div>;
    const bullet = line.match(/^(?:[-*]|\d+\.)\s+(.+)/);
    if (bullet) return <div className="document-bullet" key={i}><span>•</span><div>{inline(bullet[1])}</div></div>;
    return line.trim() ? <p key={i}>{inline(line)}</p> : <div className="document-space" key={i} />;
  })}</div>;
}
