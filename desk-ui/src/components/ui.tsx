import { Dialog as D } from "radix-ui";
import { X } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "../cn";

type Tone = "primary" | "quiet" | "danger" | "outline-danger";

const tones: Record<Tone, string> = {
  primary: "bg-accent text-accent-ink hover:opacity-90",
  quiet: "border border-rule bg-surface text-ink hover:bg-sunk",
  danger: "bg-halt text-white hover:opacity-90",
  "outline-danger": "border-[1.5px] border-halt text-halt hover:bg-halt-soft",
};

export function Button({
  tone = "quiet",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { tone?: Tone }) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex min-h-11 items-center justify-center gap-2 rounded-xl px-4 text-[15px] font-semibold transition-[opacity,background] disabled:cursor-not-allowed disabled:opacity-45",
        tones[tone],
        className,
      )}
      {...props}
    />
  );
}

const pillTones = {
  gain: "bg-gain-soft text-gain",
  loss: "bg-loss-soft text-loss",
  warn: "bg-warn-soft text-warn",
  accent: "bg-accent-soft text-accent",
  halt: "bg-halt-soft text-halt",
  neutral: "bg-sunk text-muted",
} as const;

export function Pill({ tone = "neutral", children, className }: {
  tone?: keyof typeof pillTones;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center rounded-full px-2 py-1 font-mono text-[11px] font-medium leading-none tracking-wide", pillTones[tone], className)}>
      {children}
    </span>
  );
}

export function Card({ children, className, urgent }: { children: ReactNode; className?: string; urgent?: boolean }) {
  return (
    <section
      className={cn(
        "rounded-2xl border border-rule bg-surface p-4",
        urgent && "border-accent shadow-[0_0_0_3px_var(--accent-soft)]",
        className,
      )}
    >
      {children}
    </section>
  );
}

export function Label({ children }: { children: ReactNode }) {
  return <div className="font-mono text-[11px] font-medium uppercase tracking-[0.06em] text-faint">{children}</div>;
}

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <D.Root open={open} onOpenChange={onOpenChange}>
      <D.Portal>
        <D.Overlay className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]" />
        <D.Content className="fixed inset-x-3 bottom-3 z-50 rounded-2xl border border-rule bg-surface p-5 shadow-xl outline-none pb-[max(20px,env(safe-area-inset-bottom))] sm:inset-x-auto sm:bottom-auto sm:left-1/2 sm:top-1/3 sm:w-[440px] sm:-translate-x-1/2">
          <div className="flex items-start justify-between gap-4">
            <D.Title className="text-lg font-bold tracking-tight">{title}</D.Title>
            <D.Close className="rounded-lg p-1 text-muted hover:bg-sunk" aria-label="Close">
              <X size={18} />
            </D.Close>
          </div>
          {description ? <D.Description className="mt-1 text-[14px] text-muted">{description}</D.Description> : <D.Description className="sr-only">{title}</D.Description>}
          <div className="mt-4">{children}</div>
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-2xl border border-dashed border-rule px-5 py-8 text-center">
      <div className="font-semibold">{title}</div>
      {children ? <div className="mt-1 text-[14px] text-muted">{children}</div> : null}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="rounded-xl bg-loss-soft px-4 py-3 text-[14px] text-loss">
      {msg}
    </div>
  );
}
