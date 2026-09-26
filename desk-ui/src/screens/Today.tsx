import { Link } from "@tanstack/react-router";
import { ChevronRight } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { passkeyMessage, withPasskey, type InboxItem, type MarketState, type OrderPlan, type Switches, type Today } from "../api";
import { cn } from "../cn";
import { HoldButton } from "../components/HoldButton";
import { useToast } from "../components/toast";
import { Button, Card, Empty, ErrorNote, Label, Pill } from "../components/ui";
import { day, money, remaining, time } from "../format";
import { useRefresh, useSession, useToday } from "../hooks";

const stripe: Record<InboxItem["severity"], string> = {
  critical: "bg-halt",
  action: "bg-accent",
  attention: "bg-warn",
  info: "bg-faint",
};

function useSecondsUntil(iso: string | undefined) {
  const calc = useCallback(() => (iso ? Math.max(0, Math.floor((new Date(iso).getTime() - Date.now()) / 1000)) : 0), [iso]);
  const [left, setLeft] = useState(calc);
  useEffect(() => {
    setLeft(calc());
    const t = setInterval(() => setLeft(calc()), 1000);
    return () => clearInterval(t);
  }, [calc]);
  return left;
}

function skipKey(id: string) {
  return `desk:skip:${id}`;
}

function readSkip(id: string) {
  try {
    return sessionStorage.getItem(skipKey(id)) === "1";
  } catch {
    return false;
  }
}

function PlanCard({ plan, switches }: { plan: OrderPlan; switches: Switches }) {
  const left = useSecondsUntil(plan.expires);
  const session = useSession();
  const toast = useToast();
  const refresh = useRefresh();
  const [busy, setBusy] = useState(false);
  const [skipped, setSkipped] = useState(() => readSkip(plan.id));
  const enrolled = (session.data?.passkeys ?? 0) > 0;
  const expired = left <= 0;
  const blockedBy = switches.refusal;
  const soon = left > 0 && left <= 600;

  const approve = useCallback(async () => {
    setBusy(true);
    try {
      const r = await withPasskey("submit", { plan_id: plan.id });
      toast(r.result);
      refresh();
    } catch (e) {
      toast(passkeyMessage(e), "error");
    } finally {
      setBusy(false);
    }
  }, [plan.id, refresh, toast]);

  // ⌘↵ / Ctrl+↵ approves on a keyboard; the passkey still asks.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && enrolled && !expired && !blockedBy && !busy && !skipped) {
        e.preventDefault();
        void approve();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [approve, blockedBy, busy, enrolled, expired, skipped]);

  if (skipped)
    return (
      <Card className="flex items-center justify-between gap-3 py-3">
        <span className="text-[14px] text-muted">
          Skipped. The plan expires at {time(plan.expires)} and nothing is sent.
        </span>
        <button
          type="button"
          className="text-[14px] font-semibold text-accent"
          onClick={() => {
            try {
              sessionStorage.removeItem(skipKey(plan.id));
            } catch {
              /* storage unavailable: the skip just ends here */
            }
            setSkipped(false);
          }}
        >
          Undo
        </button>
      </Card>
    );

  return (
    <Card urgent={!expired && !blockedBy}>
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-[16px] font-bold">Order plan · {plan.strategy}</h2>
        <span
          className={cn(
            "num whitespace-nowrap rounded-full px-2.5 py-1 text-[12px] font-medium",
            expired ? "bg-sunk text-muted" : soon ? "bg-warn-soft text-warn" : "bg-accent-soft text-accent",
          )}
        >
          {expired ? "expired" : remaining(left)}
        </span>
      </div>
      <p className="mt-1 text-[13px] text-muted">
        For the {day(plan.session)} open · market-on-open · cutoff {time(plan.expires)} New York
      </p>

      <ul className="mt-3 divide-y divide-rule border-y border-rule">
        {plan.orders.map((o) => (
          <li key={`${o.side}-${o.symbol}`} className="grid grid-cols-[44px_1fr_auto] items-center gap-3 py-2.5">
            <Pill tone={o.side === "buy" ? "gain" : "loss"} className="justify-center">
              {o.side.toUpperCase()}
            </Pill>
            <span className="min-w-0">
              <span className="font-semibold">{o.symbol}</span>
              <span className="block truncate text-[12px] text-muted">{o.reason}</span>
            </span>
            <span className="num text-[13px]">{o.qty.toLocaleString()} sh</span>
          </li>
        ))}
      </ul>

      <div className="mt-4 space-y-2">
        {expired ? (
          <p className="text-[14px] text-muted">This plan expired at the cutoff. Nothing was sent.</p>
        ) : blockedBy ? (
          <p className="rounded-xl bg-halt-soft px-3 py-2.5 text-[14px] text-halt">{blockedBy}</p>
        ) : !enrolled ? (
          <Link to="/security" className="block">
            <Button tone="primary" className="w-full">
              Enrol a passkey to approve
            </Button>
          </Link>
        ) : (
          <HoldButton
            label={`Hold to approve ${plan.orders.length} order${plan.orders.length === 1 ? "" : "s"}`}
            busyLabel="Waiting for your passkey…"
            busy={busy}
            onConfirm={approve}
          />
        )}
        <div className="flex gap-2">
          <Link to="/plan/$id" params={{ id: plan.id }} className="flex-1">
            <Button className="w-full">Details</Button>
          </Link>
          {!expired ? (
            <Button
              className="flex-1"
              onClick={() => {
                try {
                  sessionStorage.setItem(skipKey(plan.id), "1");
                } catch {
                  /* storage unavailable: skip for this view only */
                }
                setSkipped(true);
              }}
            >
              Skip today
            </Button>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

function InboxRow({ item }: { item: InboxItem }) {
  const body = (
    <div className="flex items-start gap-3">
      <span className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", stripe[item.severity])} />
      <div className="min-w-0 flex-1">
        <div className="text-[15px] font-semibold">{item.title}</div>
        {item.detail ? <div className="mt-0.5 text-[13px] text-muted">{item.detail}</div> : null}
      </div>
      {item.kind === "halted" || item.kind === "blocked" || item.kind === "suggestion" ? (
        <ChevronRight size={18} className="mt-0.5 shrink-0 text-faint" />
      ) : null}
    </div>
  );
  if (item.kind === "halted" || item.kind === "blocked")
    return (
      <Link to="/book">
        <Card className="hover:bg-sunk">{body}</Card>
      </Link>
    );
  if (item.kind === "suggestion")
    return (
      <a href="/classic#lab">
        <Card className="hover:bg-sunk">{body}</Card>
      </a>
    );
  return <Card>{body}</Card>;
}

const shapeText: Record<MarketState["shape"], string> = {
  breakout_up: "Breakout up",
  channel_up: "Up channel",
  range: "Range",
  channel_down: "Down channel",
  breakout_down: "Breakout down",
  unclear: "Unclear",
};
const trendTone = { up: "gain", mixed: "neutral", down: "loss" } as const;
const pct = (x: number | null, signed = false) =>
  x == null ? "—" : `${signed && x > 0 ? "+" : ""}${(x * 100).toFixed(signed ? 1 : 0)}%`;

function MarketView({ name, m }: { name: string; m: MarketState }) {
  return (
    <div className="min-w-0 flex-1">
      <Label>
        {name} · {m.for.join(", ")}
      </Label>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <Pill tone={trendTone[m.trend]}>{m.trend === "mixed" ? "trend mixed" : `${m.trend}trend`}</Pill>
        <Pill tone={m.shape.startsWith("breakout") ? "accent" : "neutral"}>{shapeText[m.shape]}</Pill>
        {m.stressed ? <Pill tone="warn">stressed</Pill> : null}
      </div>
      <div className="mt-1.5 text-[13px] text-muted">
        {m.days} day{m.days === 1 ? "" : "s"} in this shape · vol {pct(m.vol)} · {pct(m.drawdown, true)} from high ·{" "}
        breadth {pct(m.breadth)}
      </div>
    </div>
  );
}

function MarketCard({ market }: { market: NonNullable<Today["market"]> }) {
  const asOf = market.long?.as_of ?? market.short?.as_of;
  return (
    <Card>
      <div className="flex items-baseline justify-between">
        <div className="text-[15px] font-semibold">Market state</div>
        {asOf ? <div className="text-[12px] text-faint">SPY close {day(asOf)}</div> : null}
      </div>
      <div className="mt-3 flex flex-col gap-4 sm:flex-row">
        {market.long ? <MarketView name="Long view" m={market.long} /> : null}
        {market.short ? <MarketView name="Short view" m={market.short} /> : null}
      </div>
    </Card>
  );
}

export function TodayScreen() {
  const { data, error, isPending } = useToday();
  const now = new Date();

  return (
    <div className="space-y-3">
      <div className="mb-2">
        <h1 className="text-[26px] font-bold tracking-tight">Today</h1>
        <p className="text-[14px] text-muted">
          {now.toLocaleDateString("en-US", { weekday: "long", day: "numeric", month: "long" })}
          {data?.night?.finished ? ` · night finished ${time(data.night.finished)}` : ""}
        </p>
      </div>

      {error ? <ErrorNote error={error} /> : null}
      {isPending ? (
        <div className="space-y-3" aria-busy>
          <div className="h-44 animate-pulse rounded-2xl bg-sunk" />
          <div className="h-16 animate-pulse rounded-2xl bg-sunk" />
        </div>
      ) : null}

      {data ? (
        <>
          {data.inbox
            .filter((i) => i.severity === "critical")
            .map((i) => (
              <InboxRow key={`${i.kind}-${i.title}`} item={i} />
            ))}
          {data.plan ? <PlanCard plan={data.plan} switches={data.switches} /> : null}
          {data.inbox
            .filter((i) => i.severity !== "critical" && i.kind !== "plan")
            .map((i) => (
              <InboxRow key={`${i.kind}-${i.title}`} item={i} />
            ))}
          {data.market ? <MarketCard market={data.market} /> : null}
          {data.inbox.length === 0 ? (
            <Empty title="Nothing needs you">No plan to approve, no mismatches, and last night ran cleanly.</Empty>
          ) : null}
          {!data.plan && data.inbox.every((i) => i.severity !== "critical") ? (
            <p className="px-1 pt-2 text-[13px] text-faint">
              No order plan waiting. Plans appear here after the nightly run, before the 09:28 cutoff.
            </p>
          ) : null}
          {data.plan ? (
            <p className="hidden px-1 text-[12px] text-faint md:block">
              Equity at planning {money(data.plan.equity)} · ⌘↵ approves (your passkey still asks)
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
