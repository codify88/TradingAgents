import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Markdown for tool output, reports and docs. react-markdown never renders raw HTML. */
export function Markdownish({ text, className }: { text: string; className?: string }) {
  return (
    <div className={`md text-[13.5px] leading-relaxed ${className ?? ""}`}>
      <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
    </div>
  );
}
