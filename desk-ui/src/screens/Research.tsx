import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { ChevronRight, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "../cn";
import { Markdownish } from "../components/Md";
import { useToast } from "../components/toast";
import { Button, Card, Dialog, Empty, ErrorNote, Label, Pill } from "../components/ui";
import {
  estimateResearch,
  getBookDecisions,
  getBookReport,
  getFetch,
  getOverview,
  getResearchMeta,
  getResearchReport,
  MANDATE_LABEL,
  searchSymbols,
  startFetch,
  startResearchRun,
  type FetchData,
  type ResearchRun,
  type Section,
  type SectionStatus,
  type SymbolHit,
} from "../research";

const input = "w-full rounded-xl border border-rule bg-ground px-3 py-2.5 text-[14px] outline-none focus:border-accent";
const today = () => new Date().toLocaleDateString("en-CA");
/** The latest weekday: the default as-of date, since a weekend has no session to read. */
const lastWeekday = () => {
  const d = new Date();
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1);
  return d.toLocaleDateString("en-CA");
};
const usd = (x: number | null | undefined) => (x == null ? "—" : `$${x.toFixed(2)}`);

const STATUS_TONE: Record<SectionStatus, "gain" | "neutral" | "warn" | "loss" | "accent"> = {
  ok: "gain",
  empty: "neutral",
  unavailable: "warn",
  error: "loss",
  pending: "accent",
};

const RATING_TONE: Record<string, "gain" | "loss" | "neutral"> = {
  Buy: "gain",
  Overweight: "gain",
  Hold: "neutral",
  Underweight: "loss",
  Sell: "loss",
};

/** Vendors return prices and statements as CSV under "# " comment lines: show those as a table. */
function csvOf(text: string): { notes: string[]; head: string[]; rows: string[][] } | null {
  const lines = text.split("\n").map((l) => l.trimEnd()).filter(Boolean);
  const notes = lines.filter((l) => l.startsWith("#")).map((l) => l.replace(/^#+\s*/, ""));
  const body = lines.filter((l) => !l.startsWith("#"));
  if (body.length < 2) return null;
  const width = body[0].split(",").length;
  if (width < 3 || !body.every((l) => l.split(",").length === width)) return null;
  const [head, ...rows] = body.map((l) => l.split(","));
  return { notes, head, rows: rows.reverse() };
}

function Csv({ t }: { t: NonNullable<ReturnType<typeof csvOf>> }) {
  const fmt = (v: string) => {
    const n = Number(v);
    return v !== "" && Number.isFinite(n) && !/^\d{4}-\d{2}-\d{2}/.test(v) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v;
  };
  return (
    <div className="md text-[13px]">
      {t.notes.map((n) => (
        <p key={n} className="text-[12px] text-faint">{n}</p>
      ))}
      <table>
        <thead>
          <tr>{t.head.map((h) => <th key={h}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {t.rows.map((r, i) => (
            <tr key={i}>{r.map((v, j) => <td key={j} className={j ? "text-right" : ""}>{fmt(v)}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Md({ text }: { text: string }) {
  const csv = csvOf(text);
  if (csv) return <Csv t={csv} />;
  return <Markdownish text={text} />;
}

// --- symbol search ---------------------------------------------------------------

function SymbolSearch({ onPick }: { onPick: (s: string) => void }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [debounced, setDebounced] = useState("");
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 180);
    return () => clearTimeout(t);
  }, [q]);
  const hits = useQuery({ queryKey: ["research", "search", debounced], queryFn: () => searchSymbols(debounced), enabled: !!debounced });
  useEffect(() => {
    const close = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  function pick(s: string) {
    setQ("");
    setOpen(false);
    onPick(s);
  }

  const results = hits.data?.results ?? [];
  return (
    <div ref={box} className="relative">
      <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" />
      <input
        aria-label="Search a company, ETF or index"
        className={cn(input, "pl-9")}
        placeholder="Company, ticker, ETF or index (e.g. Microsoft, NVDA, ^GSPC)"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            pick(results[0]?.symbol ?? q.trim().toUpperCase());
          }
          if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && debounced && results.length ? (
        <ul className="absolute z-20 mt-1 max-h-80 w-full overflow-auto rounded-xl border border-rule bg-surface py-1 shadow-lg">
          {results.map((h: SymbolHit) => (
            <li key={h.symbol}>
              <button type="button" className="flex w-full items-baseline gap-3 px-3 py-2 text-left hover:bg-sunk" onClick={() => pick(h.symbol)}>
                <span className="num w-20 shrink-0 font-semibold">{h.symbol}</span>
                <span className="truncate text-[14px]">{h.name}</span>
                <span className="ml-auto shrink-0 font-mono text-[11px] text-faint">{[h.type, h.exchange].filter(Boolean).join(" · ")}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// --- the screen ------------------------------------------------------------------

export function ResearchScreen() {
  const { s } = useSearch({ from: "/shell/research" });
  const navigate = useNavigate({ from: "/research" });
  const meta = useQuery({ queryKey: ["research", "meta"], queryFn: getResearchMeta, staleTime: Infinity });
  const choose = (sym: string) => navigate({ search: { s: sym.toUpperCase() } });

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-[26px] font-semibold tracking-tight">Research</h1>
        <p className="text-[14px] text-muted">
          Any company, ETF or index: pull everything the agents can read, run the agents on it outside the book, or both.
        </p>
      </div>
      <SymbolSearch onPick={choose} />
      {meta.data ? (
        <div className="flex flex-wrap gap-2">
          {meta.data.indices.map((i) => (
            <button
              key={i.symbol}
              type="button"
              onClick={() => choose(i.symbol)}
              className={cn("rounded-full border border-rule px-3 py-1 text-[12.5px] hover:bg-sunk", s === i.symbol && "border-accent text-accent")}
              title={i.name}
            >
              {i.name}
            </button>
          ))}
        </div>
      ) : null}
      {s ? <SymbolView key={s} symbol={s} /> : <Empty title="Pick something to research">Search above, or start with an index.</Empty>}
    </div>
  );
}

function SymbolView({ symbol }: { symbol: string }) {
  const qc = useQueryClient();
  const ov = useQuery({
    queryKey: ["research", "overview", symbol],
    queryFn: () => getOverview(symbol),
    refetchInterval: (q) =>
      q.state.data?.fetching.length ? 3_000 : q.state.data?.runs.some((r) => r.status === "running") ? 20_000 : false,
    refetchIntervalInBackground: true,
  });
  const [day, setDay] = useState<string | null>(null);
  const shown = day ?? ov.data?.fetches[0]?.date ?? null;

  if (ov.error) return <ErrorNote error={ov.error} />;
  const name = ov.data?.name;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="num text-[22px] font-semibold">{symbol}</h2>
        <span className="text-[16px]">{name?.name ?? (ov.data ? "Not in the stored listing — tools will still try it" : "")}</span>
        {name ? <span className="font-mono text-[11.5px] text-faint">{[name.type, name.exchange].filter(Boolean).join(" · ")}</span> : null}
      </div>

      <ActionCard
        symbol={symbol}
        onFetched={(d) => {
          setDay(d);
          qc.invalidateQueries({ queryKey: ["research", "overview", symbol] });
        }}
        onRun={() => qc.invalidateQueries({ queryKey: ["research", "overview", symbol] })}
      />

      <div className="grid gap-5 lg:grid-cols-[1fr_340px]">
        <div className="min-w-0 space-y-5">
          {shown ? (
            <DataView
              symbol={symbol}
              day={shown}
              dates={ov.data?.fetches.map((f) => f.date) ?? []}
              fetching={ov.data?.fetching.includes(shown) ?? false}
              onDay={setDay}
            />
          ) : (
            <Card>
              <Label>Data</Label>
              <p className="mt-2 text-[14px] text-muted">
                Nothing fetched for {symbol} yet. <span className="text-faint">Fetching costs vendor requests only, no model calls, and takes seconds.</span>
              </p>
            </Card>
          )}
        </div>
        <div className="space-y-5">
          <RunsCard symbol={symbol} runs={ov.data?.runs ?? []} />
          <BookCard symbol={symbol} />
        </div>
      </div>
    </div>
  );
}

// --- fetch / run controls --------------------------------------------------------

type Mode = "data" | "agents" | "both";

function ActionCard({ symbol, onFetched, onRun }: { symbol: string; onFetched: (day: string) => void; onRun: () => void }) {
  const meta = useQuery({ queryKey: ["research", "meta"], queryFn: getResearchMeta, staleTime: Infinity });
  const toast = useToast();
  const [mode, setMode] = useState<Mode>("data");
  const [date, setDate] = useState(lastWeekday());
  const [mandate, setMandate] = useState("");
  const [deep, setDeep] = useState("");
  const [quick, setQuick] = useState("");
  const [agree, setAgree] = useState(false);
  const [busy, setBusy] = useState(false);
  const models = useMemo(() => ({ ...(deep ? { deep } : {}), ...(quick ? { quick } : {}) }), [deep, quick]);
  const agents = mode !== "data";
  const est = useQuery({
    queryKey: ["research", "estimate", models],
    queryFn: () => estimateResearch(models),
    enabled: agents,
  });
  useEffect(() => setAgree(false), [models, mandate, mode]);

  async function go() {
    setBusy(true);
    try {
      if (mode !== "agents") {
        const r = await startFetch(symbol, date);
        toast(r.result);
        onFetched(r.date);
      }
      if (agents && est.data?.cost_high != null) {
        const r = await startResearchRun(symbol, { date, mandate, models, confirm_cost: est.data.cost_high });
        toast(r.result, r.result.startsWith("Refused") ? "error" : undefined);
        onRun();
        setAgree(false);
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy(false);
    }
  }

  const def = meta.data?.default_models;
  const label = { data: "Fetch all data", agents: "Run the agents", both: "Fetch data and run the agents" }[mode];
  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3">
        <div role="radiogroup" aria-label="What to do" className="flex rounded-xl border border-rule bg-ground p-1">
          {(
            [
              ["data", "Data"],
              ["agents", "Agents"],
              ["both", "Both"],
            ] as const
          ).map(([k, l]) => (
            <button
              key={k}
              type="button"
              role="radio"
              aria-checked={mode === k}
              onClick={() => setMode(k)}
              className={cn("rounded-lg px-3.5 py-1.5 text-[14px] font-medium", mode === k ? "bg-surface shadow-sm" : "text-muted")}
            >
              {l}
            </button>
          ))}
        </div>
        <div>
          <label htmlFor="rs-date" className="block text-[12.5px] text-muted">As of</label>
          <input id="rs-date" type="date" max={today()} className={cn(input, "mt-1 py-2")} value={date} onChange={(e) => setDate(e.target.value)} />
        </div>
      </div>

      <p className="mt-3 text-[13px] text-muted">
        {mode === "data"
          ? "Every tool the agents and the mandates can call — prices and technicals, statements, the latest earnings call, news, insiders, Congress, 13F holders, ETFs, macro, and each mandate's own metrics. No model calls."
          : "The full pipeline — analysts, bull and bear debate, trader, risk, portfolio manager — on this one name. It runs apart from the book: nothing is logged as a decision or traded."}
      </p>

      {agents ? (
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <div>
            <label htmlFor="rs-mandate" className="text-[12.5px] text-muted">Mandate</label>
            <select id="rs-mandate" className={cn(input, "mt-1")} value={mandate} onChange={(e) => setMandate(e.target.value)}>
              {["", ...(meta.data?.mandates ?? [])].map((m) => (
                <option key={m} value={m}>{MANDATE_LABEL[m] ?? m}</option>
              ))}
            </select>
          </div>
          {(
            [
              ["deep", deep, setDeep],
              ["quick", quick, setQuick],
            ] as const
          ).map(([k, v, set]) => (
            <div key={k}>
              <label htmlFor={`rs-${k}`} className="text-[12.5px] text-muted">{k === "deep" ? "Deep model (managers)" : "Quick model (analysts)"}</label>
              <select id={`rs-${k}`} className={cn(input, "mt-1")} value={v} onChange={(e) => set(e.target.value)}>
                <option value="">Default ({def?.[k] ?? "…"})</option>
                {meta.data?.models.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </div>
          ))}
        </div>
      ) : null}

      <div className="mt-4 space-y-3">
        {agents && est.data ? (
          <label className="flex items-center gap-2 text-[14px]">
            <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
            Spend up to <span className="num font-semibold">{usd(est.data.cost_high)}</span> of API credits
            <span className="text-faint">(one decision, ~{est.data.minutes} min)</span>
          </label>
        ) : null}
        <Button tone="primary" disabled={busy || !date || (agents && (!agree || est.data?.cost_high == null))} onClick={go}>
          {busy ? "Starting…" : label}
        </Button>
        {agents ? <p className="text-[12px] text-faint">Agent runs are background jobs; they are refused between 01:30 and 07:30, when the nightly run owns the budget.</p> : null}
      </div>
    </Card>
  );
}

// --- data ------------------------------------------------------------------------

function DataView({ symbol, day, dates, fetching, onDay }: { symbol: string; day: string; dates: string[]; fetching: boolean; onDay: (d: string) => void }) {
  const meta = useQuery({ queryKey: ["research", "meta"], queryFn: getResearchMeta, staleTime: Infinity });
  const qc = useQueryClient();
  // Until the server saves the first sections the file doesn't exist: that's "not yet", not an error.
  const data = useQuery({
    queryKey: ["research", "data", symbol, day],
    queryFn: () => getFetch(symbol, day).catch((e) => (fetching ? null : Promise.reject(e))),
    refetchInterval: (q) => (fetching || !q.state.data || q.state.data.status === "running" ? 1_500 : false),
    refetchIntervalInBackground: true,
    retry: 0,
  });
  const finished = data.data?.status === "finished";
  useEffect(() => {
    if (finished) qc.invalidateQueries({ queryKey: ["research", "overview", symbol] });
  }, [finished, qc, symbol]);
  const [hideEmpty, setHideEmpty] = useState(true);

  const d: FetchData | undefined = data.data ?? undefined;
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    d?.sections.forEach((x) => (c[x.status] = (c[x.status] ?? 0) + 1));
    return c;
  }, [d]);

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3">
        <Label>Data as of</Label>
        <select aria-label="Fetched date" className="rounded-lg border border-rule bg-ground px-2 py-1 text-[13px]" value={day} onChange={(e) => onDay(e.target.value)}>
          {(dates.includes(day) ? dates : [day, ...dates]).map((x) => (
            <option key={x} value={x}>{x}</option>
          ))}
        </select>
        {d?.status === "running" || (fetching && !finished) ? <Pill tone="accent">fetching {d ? `${d.sections.filter((x) => x.status !== "pending").length}/${d.sections.length}` : ""}</Pill> : null}
        <span className="flex flex-wrap gap-1.5">
          {(["ok", "empty", "unavailable", "error"] as const).map((k) =>
            counts[k] ? (
              <Pill key={k} tone={STATUS_TONE[k]}>
                {counts[k]} {k}
              </Pill>
            ) : null,
          )}
        </span>
        <label className="ml-auto flex items-center gap-1.5 text-[12.5px] text-muted">
          <input type="checkbox" checked={hideEmpty} onChange={(e) => setHideEmpty(e.target.checked)} /> Hide empty
        </label>
      </div>
      {data.error && !fetching ? <ErrorNote error={data.error} /> : null}
      {d ? (
        <div className="mt-3 space-y-4">
          {(meta.data?.groups ?? []).map((g) => {
            const rows = d.sections.filter((x) => x.group === g.key && (!hideEmpty || x.status === "ok" || x.status === "error" || x.status === "pending"));
            if (!rows.length) return null;
            return (
              <div key={g.key}>
                <div className="mb-1 text-[13px] font-semibold text-muted">{g.title}</div>
                <div className="divide-y divide-rule overflow-hidden rounded-xl border border-rule">
                  {rows.map((x) => (
                    <SectionRow key={x.key} x={x} />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
    </Card>
  );
}

function SectionRow({ x }: { x: Section }) {
  const [open, setOpen] = useState(false);
  const [raw, setRaw] = useState(false);
  const preview = x.text.split("\n").find((l) => l.trim() && !l.startsWith("#") && !l.startsWith("|--"))?.slice(0, 140) ?? "";
  return (
    <div>
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-sunk/60" aria-expanded={open}>
        <ChevronRight size={15} className={cn("shrink-0 text-faint transition-transform", open && "rotate-90")} />
        <span className="shrink-0 text-[14px] font-medium">{x.title}</span>
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-faint">{open ? "" : preview}</span>
        <Pill tone={STATUS_TONE[x.status]}>{x.status}</Pill>
      </button>
      {open ? (
        <div className="border-t border-rule bg-ground/60 px-4 py-3">
          <div className="mb-2 flex flex-wrap items-center gap-3 font-mono text-[11px] text-faint">
            <span>{x.tool}</span>
            {x.vendor ? <span>via {x.vendor}</span> : null}
            <span>{x.seconds}s</span>
            <button type="button" className="ml-auto underline" onClick={() => setRaw(!raw)}>
              {raw ? "formatted" : "raw"}
            </button>
          </div>
          <div className="max-h-[560px] overflow-auto">
            {raw ? <pre className="whitespace-pre-wrap break-words font-mono text-[12px]">{x.text}</pre> : <Md text={x.text} />}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// --- agent runs and the book's decisions -----------------------------------------

function RunsCard({ symbol, runs }: { symbol: string; runs: ResearchRun[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const rep = useQuery({ queryKey: ["research", "report", symbol, open], queryFn: () => getResearchReport(symbol, open!), enabled: !!open });
  return (
    <Card>
      <Label>Agent runs</Label>
      {runs.length === 0 ? <p className="mt-2 text-[13px] text-faint">None yet. Choose Agents or Both above.</p> : null}
      <ul className="mt-2 divide-y divide-rule">
        {runs.map((r) => (
          <li key={r.id}>
            <button type="button" className="flex w-full items-center gap-2 py-2.5 text-left disabled:cursor-default" disabled={r.status === "running"} onClick={() => setOpen(r.id)}>
              <span className="min-w-0 flex-1">
                <span className="block text-[14px] font-medium">{MANDATE_LABEL[r.mandate] ?? r.mandate}</span>
                <span className="num block text-[11.5px] text-faint">
                  as of {r.date} · {[r.models.deep, r.models.quick].filter(Boolean).join(" / ") || "default models"}
                </span>
              </span>
              {r.status === "running" ? (
                <Pill tone="accent">running</Pill>
              ) : r.status === "failed" ? (
                <Pill tone="loss">failed</Pill>
              ) : (
                <Pill tone={RATING_TONE[r.rating ?? ""] ?? "neutral"}>{r.rating}</Pill>
              )}
            </button>
          </li>
        ))}
      </ul>
      <Dialog wide open={!!open} onOpenChange={(o) => !o && setOpen(null)} title={`${symbol} · ${MANDATE_LABEL[rep.data?.mandate ?? ""] ?? ""}`} description={rep.data ? `As of ${rep.data.date}${rep.data.rating ? ` · rated ${rep.data.rating}` : ""}` : ""}>
        <div className="max-h-[70vh] overflow-auto">
          {rep.data?.error ? <p className="text-[13px] text-loss">{rep.data.error}</p> : null}
          {rep.data ? rep.data.report ? <Md text={rep.data.report} /> : <p className="text-[13px] text-faint">No report was written.</p> : <p className="text-[13px] text-faint">Loading…</p>}
        </div>
      </Dialog>
    </Card>
  );
}

function BookCard({ symbol }: { symbol: string }) {
  const q = useQuery({ queryKey: ["research", "book", symbol], queryFn: () => getBookDecisions(symbol) });
  const [open, setOpen] = useState<string | null>(null);
  const rep = useQuery({ queryKey: ["research", "bookreport", symbol, open], queryFn: () => getBookReport(symbol, open!), enabled: !!open });
  const rows = q.data?.decisions ?? [];
  return (
    <Card>
      <Label>The book's decisions</Label>
      {q.data && rows.length === 0 ? <p className="mt-2 text-[13px] text-faint">The nightly runs haven't decided {symbol}.</p> : null}
      <ul className="mt-2 divide-y divide-rule">
        {rows.map((r) => (
          <li key={`${r.date}-${r.mandate}`}>
            <button type="button" className="flex w-full items-center gap-2 py-2.5 text-left" onClick={() => setOpen(r.date)}>
              <span className="min-w-0 flex-1">
                <span className="num block text-[14px]">{r.date}</span>
                <span className="block text-[11.5px] text-faint">
                  {MANDATE_LABEL[r.mandate ?? ""] ?? r.mandate} · {r.pending ? "in progress" : `alpha ${r.alpha ?? "—"}`}
                </span>
              </span>
              <Pill tone={RATING_TONE[r.rating] ?? "neutral"}>{r.rating}</Pill>
            </button>
          </li>
        ))}
      </ul>
      <Dialog wide open={!!open} onOpenChange={(o) => !o && setOpen(null)} title={`${symbol} · ${open ?? ""}`} description="The nightly decision's report">
        <div className="max-h-[70vh] overflow-auto">{rep.data ? <Md text={rep.data.text} /> : <p className="text-[13px] text-faint">Loading…</p>}</div>
      </Dialog>
    </Card>
  );
}
