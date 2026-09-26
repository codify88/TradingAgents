import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { BookOpen, FlaskConical, KeyRound, LayoutList, Octagon, ScrollText } from "lucide-react";
import { useState, type ReactNode } from "react";
import { halt } from "../api";
import { cn } from "../cn";
import { useRefresh, useToday } from "../hooks";
import { useToast } from "./toast";
import { Button, Dialog, Pill } from "./ui";

type NavItem = { to: string; label: string; icon: ReactNode; external?: boolean };

// Lab and Research open the v1 page until their screens land (D3, D4).
const NAV: NavItem[] = [
  { to: "/", label: "Today", icon: <LayoutList size={18} /> },
  { to: "/book", label: "Book", icon: <BookOpen size={18} /> },
  { to: "/classic#lab", label: "Lab", icon: <FlaskConical size={18} />, external: true },
  { to: "/classic#decisions", label: "Research", icon: <ScrollText size={18} />, external: true },
  { to: "/security", label: "Security", icon: <KeyRound size={18} /> },
];

function NavLink({ item, compact }: { item: NavItem; compact?: boolean }) {
  const path = useRouterState({ select: (s) => s.location.pathname });
  const active = !item.external && (item.to === "/" ? path === "/" || path.startsWith("/plan") : path.startsWith(item.to));
  const cls = compact
    ? cn("flex flex-1 flex-col items-center gap-1 py-1 text-[11px] font-medium", active ? "text-accent" : "text-faint")
    : cn(
        "flex items-center gap-3 rounded-lg px-3 py-2 text-[14px] font-medium",
        active ? "bg-accent-soft text-accent" : "text-muted hover:bg-sunk hover:text-ink",
      );
  if (item.external)
    return (
      <a href={item.to} className={cls}>
        {item.icon}
        {item.label}
      </a>
    );
  return (
    <Link to={item.to} className={cls}>
      {item.icon}
      {item.label}
    </Link>
  );
}

function HaltControl({ wide }: { wide?: boolean }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const refresh = useRefresh();
  const today = useToday();
  const halted = today.data?.switches.halted;

  if (halted)
    return (
      <Link to="/book" className="shrink-0">
        <Pill tone="halt" className="px-3 py-2 text-[12px]">HALTED</Pill>
      </Link>
    );

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "inline-flex shrink-0 items-center gap-2 rounded-full border-[1.5px] border-halt font-semibold uppercase tracking-wide text-halt hover:bg-halt-soft",
          wide ? "w-full justify-center px-3 py-2 text-[13px]" : "px-3 py-1.5 text-[11px]",
        )}
      >
        <Octagon size={wide ? 16 : 13} /> Halt{wide ? " trading" : ""}
      </button>
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="Halt trading?"
        description="Every new order is refused and open orders are cancelled. Positions are kept. Resuming needs your passkey."
      >
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            try {
              const r = await halt(reason.trim() || "operator via Desk");
              toast(r.result);
              setOpen(false);
              setReason("");
              refresh();
            } catch (err) {
              toast(err instanceof Error ? err.message : String(err), "error");
            } finally {
              setBusy(false);
            }
          }}
          className="space-y-3"
        >
          <label className="block text-[14px] text-muted" htmlFor="halt-reason">
            Reason (optional)
          </label>
          <input
            id="halt-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. market open looks wrong"
            className="w-full rounded-xl border border-rule bg-ground px-3 py-3 text-[15px] outline-none focus:border-accent"
          />
          <Button tone="danger" type="submit" disabled={busy} className="w-full">
            {busy ? "Halting…" : "Halt trading"}
          </Button>
        </form>
      </Dialog>
    </>
  );
}

export function Shell() {
  const today = useToday();
  const needs = today.data?.needs_you ?? 0;
  return (
    <div className="flex h-full">
      <aside className="hidden w-56 shrink-0 flex-col gap-1 border-r border-rule bg-surface px-3 py-5 md:flex">
        <div className="px-3 pb-4 text-[17px] font-bold tracking-tight">Desk</div>
        {NAV.map((n) => (
          <div key={n.to} className="relative">
            <NavLink item={n} />
            {n.to === "/" && needs > 0 ? (
              <span className="num absolute right-3 top-2 rounded-full bg-accent px-1.5 text-[11px] leading-5 text-accent-ink">
                {needs}
              </span>
            ) : null}
          </div>
        ))}
        <div className="flex-1" />
        <HaltControl wide />
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center justify-between gap-3 border-b border-rule bg-ground/90 px-4 pb-3 pt-[max(12px,env(safe-area-inset-top))] backdrop-blur md:hidden">
          <span className="text-[17px] font-bold tracking-tight">Desk</span>
          <HaltControl />
        </header>
        <main className="flex-1 overflow-y-auto px-4 pb-[calc(88px+env(safe-area-inset-bottom))] pt-4 md:px-8 md:pb-10 md:pt-8">
          <div className="mx-auto max-w-3xl">
            <Outlet />
          </div>
        </main>
        <nav className="fixed inset-x-0 bottom-0 z-30 flex border-t border-rule bg-surface px-2 pb-[max(10px,env(safe-area-inset-bottom))] pt-2 md:hidden">
          {NAV.map((n) => (
            <NavLink key={n.to} item={n} compact />
          ))}
        </nav>
      </div>
    </div>
  );
}
