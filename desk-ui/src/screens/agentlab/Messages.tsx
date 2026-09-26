import { useState } from "react";
import type { Message } from "../../agentlab";
import { cn } from "../../cn";

const ROLE: Record<string, string> = { system: "System prompt", human: "Message", ai: "Model" };

/** A captured prompt: each message collapsible, long ones clipped until opened. */
export function Messages({ messages, highlight }: { messages: Message[]; highlight?: string[] }) {
  return (
    <div className="mt-2 space-y-2">
      {messages.map((m, i) => (
        <One key={i} m={m} highlight={highlight} />
      ))}
    </div>
  );
}

function One({ m, highlight }: { m: Message; highlight?: string[] }) {
  const long = m.text.length > 1400;
  const [open, setOpen] = useState(!long);
  const text = open ? m.text : `${m.text.slice(0, 1400)}…`;
  return (
    <div className="rounded-xl border border-rule">
      <div className="flex items-center justify-between border-b border-rule bg-sunk/60 px-3 py-1.5">
        <span className="text-[12px] font-semibold uppercase tracking-wide text-muted">{ROLE[m.role] ?? m.role}</span>
        <span className="num text-[11px] text-faint">{m.text.length.toLocaleString()} chars</span>
      </div>
      <pre className="max-h-[480px] overflow-auto whitespace-pre-wrap break-words px-3 py-2 font-mono text-[12px] leading-relaxed text-ink">
        <Highlighted text={text} marks={highlight ?? []} />
      </pre>
      {long ? (
        <button type="button" onClick={() => setOpen(!open)} className="w-full border-t border-rule py-1.5 text-[12px] font-semibold text-accent">
          {open ? "Show less" : "Show all"}
        </button>
      ) : null}
    </div>
  );
}

/** Mark each occurrence of the given passages (the edits a variant adds). */
function Highlighted({ text, marks }: { text: string; marks: string[] }) {
  const ms = marks.map((s) => s.trim()).filter((s) => s.length > 3);
  if (!ms.length) return <>{text}</>;
  const parts: { t: string; hit: boolean }[] = [];
  let rest = text;
  while (rest) {
    let best = -1;
    let len = 0;
    for (const s of ms) {
      const at = rest.indexOf(s);
      if (at !== -1 && (best === -1 || at < best)) {
        best = at;
        len = s.length;
      }
    }
    if (best === -1) {
      parts.push({ t: rest, hit: false });
      break;
    }
    if (best > 0) parts.push({ t: rest.slice(0, best), hit: false });
    parts.push({ t: rest.slice(best, best + len), hit: true });
    rest = rest.slice(best + len);
  }
  return (
    <>
      {parts.map((p, i) => (
        <span key={i} className={cn(p.hit && "rounded bg-accent-soft text-accent")}>
          {p.t}
        </span>
      ))}
    </>
  );
}
