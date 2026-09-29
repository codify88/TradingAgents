import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import {
  estimateReplay,
  getReplayCases,
  getReplayDecisions,
  getReplays,
  getVariants,
  startReplay,
  type ReplayEstimate,
  type ReplayMetrics,
  type ReplayRequest,
} from "../../agentlab";
import { cn } from "../../cn";
import { useToast } from "../../components/toast";
import { Button, Card, Dialog, Empty, ErrorNote, Label, Pill } from "../../components/ui";

const input = "mt-1 w-full rounded-xl border border-rule bg-ground px-3 py-2.5 text-[14px] outline-none focus:border-accent";
const RATINGS = ["Buy", "Overweight", "Hold", "Underweight", "Sell"] as const;
const TONE: Record<string, string> = {
  Buy: "bg-gain",
  Overweight: "bg-gain/60",
  Hold: "bg-faint/50",
  Underweight: "bg-loss/60",
  Sell: "bg-loss",
};
const pct = (x: number | null | undefined, d = 0) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const pp = (x: number | null | undefined) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(2)}%`);
const usd = (x: number | null | undefined) => (x == null ? "—" : `$${x.toFixed(x < 10 ? 2 : 0)}`);

function Mix({ r }: { r: Record<string, number> }) {
  const total = RATINGS.reduce((a, k) => a + (r[k] ?? 0), 0);
  if (!total) return <span className="text-faint">—</span>;
  return (
    <div className="flex h-2.5 w-32 overflow-hidden rounded-full bg-sunk" title={RATINGS.map((k) => `${k} ${r[k] ?? 0}`).join(", ")}>
      {RATINGS.map((k) => (r[k] ? <span key={k} className={TONE[k]} style={{ width: `${(r[k] / total) * 100}%` }} /> : null))}
    </div>
  );
}

/** Replays on the same cases, stage, horizon and models are comparable; baseline first. */
function groupKey(m: ReplayMetrics) {
  return [m.stage, m.horizon, m.models.join("+"), m.case_ids.join(",")].join("|");
}

export function ReplaysTab({ focus }: { focus: { variant?: string } }) {
  const vs = useQuery({ queryKey: ["agentlab", "variants"], queryFn: getVariants });
  const cs = useQuery({ queryKey: ["agentlab", "replay-cases"], queryFn: getReplayCases });
  const rs = useQuery({
    queryKey: ["agentlab", "replays"],
    queryFn: getReplays,
    refetchInterval: (q) => (q.state.data?.replays.some((r) => r.status === "running") ? 8_000 : false),
  });
  const qc = useQueryClient();
  const toast = useToast();
  const [req, setReq] = useState<ReplayRequest>({
    variant: focus.variant ?? "baseline",
    stage: "pm",
    mandate: "any",
    count: 40,
    seed: 7,
    horizon: 5,
  });
  const [est, setEst] = useState<ReplayEstimate | null>(null);
  const [estErr, setEstErr] = useState("");
  const [agree, setAgree] = useState(false);
  const [overCap, setOverCap] = useState(false);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<ReplayMetrics | null>(null);

  useEffect(() => {
    setAgree(false);
    setOverCap(false);
    setEst(null);
    setEstErr("");
    const t = setTimeout(() => {
      estimateReplay(req).then(setEst, (e) => setEstErr(e instanceof Error ? e.message : String(e)));
    }, 250);
    return () => clearTimeout(t);
  }, [req]);

  const set = <K extends keyof ReplayRequest>(k: K, v: ReplayRequest[K]) => setReq({ ...req, [k]: v, case_ids: undefined });

  async function launch(r: ReplayRequest, cost: number) {
    setBusy(true);
    try {
      const out = await startReplay({ ...r, confirm_cost: cost, over_cap: overCap });
      toast(out.result, out.result.startsWith("Refused") ? "error" : undefined);
      qc.invalidateQueries({ queryKey: ["agentlab", "replays"] });
      setAgree(false);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy(false);
    }
  }

  const groups = useMemo(() => {
    const g = new Map<string, ReplayMetrics[]>();
    for (const m of rs.data?.replays ?? []) g.set(groupKey(m), [...(g.get(groupKey(m)) ?? []), m]);
    return [...g.values()].map((ms) => ms.sort((a, b) => Number(b.variant === "baseline") - Number(a.variant === "baseline")));
  }, [rs.data]);

  const over = !!est?.cost && est.cost > est.max_cost;
  const mandates = Object.entries(cs.data?.by_mandate ?? {});
  const available = req.mandate === "any" ? cs.data?.total ?? 0 : Object.values(cs.data?.by_mandate[req.mandate] ?? {}).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-5">
      <Card>
        <Label>Replay a variant on saved decisions</Label>
        <p className="mt-1 text-[13px] text-muted">
          Re-runs only the last stages of past decisions under the variant, reusing their analyst reports and debates, and
          scores every case on the same horizon. Compare against <span className="font-medium">baseline</span> on the same
          case set: same filter, count and seed.
        </p>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
          <div className="lg:col-span-2">
            <label htmlFor="rp-variant" className="text-[13px] text-muted">Variant</label>
            <select id="rp-variant" className={input} value={req.variant} onChange={(e) => set("variant", e.target.value)}>
              {vs.data?.variants.map((v) => (
                <option key={v.name} value={v.name}>
                  {v.name}
                  {v.version ? ` (v${v.version})` : ""}
                </option>
              ))}
            </select>
          </div>
          <div className="lg:col-span-2">
            <label htmlFor="rp-stage" className="text-[13px] text-muted">Re-run from</label>
            <select id="rp-stage" className={input} value={req.stage} onChange={(e) => set("stage", e.target.value)}>
              {Object.entries(cs.data?.stages ?? {}).map(([k, l]) => (
                <option key={k} value={k}>{l}</option>
              ))}
            </select>
          </div>
          <div className="lg:col-span-2">
            <label htmlFor="rp-mandate" className="text-[13px] text-muted">Cases from</label>
            <select id="rp-mandate" className={input} value={req.mandate} onChange={(e) => set("mandate", e.target.value)}>
              <option value="any">Any mandate ({cs.data?.total ?? "…"})</option>
              {mandates.map(([m, r]) => (
                <option key={m} value={m}>
                  {m === "none" ? "Standard (no mandate)" : m} ({Object.values(r).reduce((a, b) => a + b, 0)})
                </option>
              ))}
            </select>
          </div>
          {(
            [
              ["count", "Cases", 1, cs.data?.max ?? 300],
              ["seed", "Seed", 0, 9999],
              ["horizon", "Horizon (sessions)", 1, 63],
            ] as const
          ).map(([k, l, min, max]) => (
            <div key={k} className="lg:col-span-2">
              <label htmlFor={`rp-${k}`} className="text-[13px] text-muted">{l}</label>
              <input id={`rp-${k}`} type="number" min={min} max={max} className={`${input} num`} value={req[k]} onChange={(e) => set(k, Number(e.target.value))} />
            </div>
          ))}
        </div>
        {req.count > available && available ? <p className="mt-2 text-[12.5px] text-warn">Only {available} saved decisions match; the replay uses them all.</p> : null}
        {estErr ? <p className="mt-3 text-[13px] text-loss">{estErr}</p> : null}
        {est ? (
          <div className="mt-4 space-y-3">
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-[14px]">
              <span><span className="text-muted">Cases </span><span className="num font-medium">{est.cases}</span></span>
              <span><span className="text-muted">Models </span><span className="num">{est.models.join(" + ")}</span></span>
              <span><span className="text-muted">Cost </span><span className="num font-medium">{usd(est.cost)}</span><span className="text-faint"> (API credits)</span></span>
              <span><span className="text-muted">Time </span><span className="num">~{est.minutes} min</span></span>
            </div>
            <label className="flex items-center gap-2 text-[14px]">
              <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
              Spend up to {usd(est.cost)} of API credits on this replay
            </label>
            {over ? (
              <label className="flex items-center gap-2 text-[14px] text-warn">
                <input type="checkbox" checked={overCap} onChange={(e) => setOverCap(e.target.checked)} />
                That is over the ${est.max_cost.toFixed(0)} cap; go over it for this replay
              </label>
            ) : null}
            <Button tone="primary" disabled={!agree || busy || est.cost == null || (over && !overCap)} onClick={() => launch(req, est.cost!)}>
              {busy ? "Starting…" : "Start the replay"}
            </Button>
            <p className="text-[12px] text-faint">Replays run as background jobs alongside anything else, but not between 01:30 and 07:30.</p>
          </div>
        ) : null}
      </Card>

      {rs.error ? <ErrorNote error={rs.error} /> : null}
      {rs.data && !rs.data.replays.length ? <Empty title="No replays yet">Start with baseline on a case set, then a knob variant on the same one.</Empty> : null}
      {groups.map((ms) => {
        const first = ms[0];
        const base = ms.find((m) => m.variant === "baseline");
        return (
          <Card key={groupKey(first)}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <Label>
                {cs.data?.stages[first.stage] ?? first.stage} · {first.cases} cases · {first.horizon}-session alpha · {first.models.join(" + ")}
              </Label>
              {!base ? (
                <button
                  type="button"
                  className="text-[12.5px] text-accent underline"
                  onClick={() => setReq({ variant: "baseline", stage: first.stage, mandate: "any", count: first.cases, seed: 7, horizon: first.horizon, case_ids: first.case_ids })}
                >
                  No baseline on these cases: set one up
                </button>
              ) : null}
            </div>
            <div className="-mx-4 mt-2 overflow-x-auto px-4">
              <table className="w-full min-w-[900px] text-[13px]">
                <thead>
                  <tr className="text-left font-mono text-[11px] uppercase tracking-wide text-faint">
                    <th className="py-2 pr-3">Variant</th>
                    <th className="pr-3">Done</th>
                    <th className="pr-3">Ratings</th>
                    <th className="pr-3" title="Share rated Buy or Overweight">Bullish</th>
                    <th className="pr-3">Hold</th>
                    <th className="pr-3" title="Mean alpha of Buy/Overweight minus the rest">Edge</th>
                    <th className="pr-3" title="Directional calls that went the called way">Hit rate</th>
                    <th className="pr-3" title="Brier score of the stated probabilities; 0.25 is a coin flip">Calibration</th>
                    <th>Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-rule">
                  {ms.map((m) => (
                    <tr key={m.id} className="cursor-pointer align-top hover:bg-sunk/50" onClick={() => setOpen(m)}>
                      <td className="py-2.5 pr-3">
                        <span className="font-semibold">{m.variant}</span>
                        {m.version ? <span className="text-faint"> v{m.version}</span> : null}
                        <span className="block text-[11px] text-faint">
                          {m.status === "running" ? "running" : m.finished?.slice(0, 16).replace("T", " ")}
                          {Object.keys(m.knobs).length ? ` · ${Object.entries(m.knobs).map(([k, v]) => `${k}=${v}`).join(", ")}` : ""}
                        </span>
                      </td>
                      <td className="num pr-3">
                        {m.done}/{m.cases}
                        {m.failed ? <span className="text-loss"> · {m.failed} failed</span> : null}
                      </td>
                      <td className="pr-3 pt-3"><Mix r={m.ratings} /></td>
                      <td className="num pr-3">
                        {pct(m.bullish_share)}
                        {base && m !== base ? <Delta a={m.bullish_share} b={base.bullish_share} /> : null}
                      </td>
                      <td className="num pr-3">
                        {pct(m.hold_share)}
                        {base && m !== base ? <Delta a={m.hold_share} b={base.hold_share} invert /> : null}
                      </td>
                      <td className={cn("num pr-3", m.new.edge != null && (m.new.edge > 0 ? "text-gain" : "text-loss"))}>
                        {pp(m.new.edge)}
                        <span className="block text-[11px] text-faint">
                          {m.new.t != null ? `t ${m.new.t.toFixed(1)} · ` : ""}{m.new.bullish_n} bullish of {m.new.scored}
                        </span>
                      </td>
                      <td className="num pr-3">
                        {pct(m.new.hit_rate)}<span className="block text-[11px] text-faint">{m.new.calls} calls</span>
                      </td>
                      <td className="num pr-3">
                        {m.probability?.brier != null ? m.probability.brier.toFixed(3) : "—"}
                        {m.probability ? <span className="block text-[11px] text-faint">mean p {pct(m.probability.mean)}</span> : null}
                      </td>
                      <td className="num">{usd(m.cost)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        );
      })}
      <p className="text-[12px] text-faint">
        A few dozen cases give a noisy edge: a t-stat under 2 is a hint. Trust a change that holds its sign across models and
        case sets, and confirm the winner on a suite with the full pipeline.
      </p>
      {open ? <ReplayDialog m={open} onClose={() => setOpen(null)} /> : null}
    </div>
  );
}

function Delta({ a, b, invert }: { a: number | null; b: number | null; invert?: boolean }) {
  if (a == null || b == null) return null;
  const d = a - b;
  if (Math.abs(d) < 0.005) return <span className="block text-[11px] text-faint">±0 vs baseline</span>;
  const good = invert ? d < 0 : d > 0;
  return <span className={cn("block text-[11px]", good ? "text-gain" : "text-loss")}>{d > 0 ? "+" : ""}{(d * 100).toFixed(0)} pts vs baseline</span>;
}

function ReplayDialog({ m, onClose }: { m: ReplayMetrics; onClose: () => void }) {
  const ds = useQuery({ queryKey: ["agentlab", "replay-decisions", m.id], queryFn: () => getReplayDecisions(m.id) });
  const [show, setShow] = useState<string | null>(null);
  const cols = [...RATINGS, "REVIEW"];
  return (
    <Dialog wide open onOpenChange={(o) => !o && onClose()} title={`${m.variant} · ${m.stage}`} description={`${m.done} of ${m.cases} cases · ${m.horizon}-session alpha vs SPY`}>
      <div className="max-h-[72vh] space-y-5 overflow-auto">
        {m.errors.length ? <p className="text-[13px] text-loss">{m.errors.join(" · ")}</p> : null}
        <div>
          <Label>Moves: original rating → replayed rating</Label>
          <table className="mt-2 text-[12.5px]">
            <thead>
              <tr className="font-mono text-[10.5px] uppercase text-faint">
                <th className="pr-3 text-left">from \ to</th>
                {cols.map((c) => (
                  <th key={c} className="px-2 text-right">{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {cols.filter((r) => m.moves[r]).map((r) => (
                <tr key={r}>
                  <td className="pr-3 font-medium">{r}</td>
                  {cols.map((c) => (
                    <td key={c} className={cn("num px-2 text-right", r === c ? "text-faint" : m.moves[r]?.[c] ? "font-semibold" : "text-faint")}>
                      {m.moves[r]?.[c] ?? "·"}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-[11.5px] text-faint">
            Original ratings came from full runs with their own lessons and sampling; judge the variant against a baseline replay, not this table alone.
          </p>
        </div>
        <div>
          <Label>Cases</Label>
          {ds.error ? <ErrorNote error={ds.error} /> : null}
          <ul className="mt-2 divide-y divide-rule">
            {ds.data?.decisions.map((d) => (
              <li key={d.case} className="py-2">
                <button type="button" className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 text-left" onClick={() => setShow(show === d.case ? null : d.case)}>
                  <span className="num w-16 font-semibold">{d.ticker}</span>
                  <span className="num text-[12px] text-faint">{d.date}</span>
                  <span className="text-[12px] text-faint">{d.mandate || "standard"}</span>
                  <span className="ml-auto flex items-center gap-2">
                    <Pill>{d.original}</Pill>→
                    <Pill tone={d.rating === "Buy" || d.rating === "Overweight" ? "gain" : d.rating === "Underweight" || d.rating === "Sell" ? "loss" : "neutral"}>{d.rating ?? d.status}</Pill>
                    {d.probability != null ? <span className="num text-[12px] text-muted">p {Math.round(d.probability * 100)}%</span> : null}
                    <span className={cn("num w-16 text-right text-[12px]", d.alpha == null ? "text-faint" : d.alpha > 0 ? "text-gain" : "text-loss")}>{pp(d.alpha)}</span>
                  </span>
                </button>
                {show === d.case ? (
                  <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-sunk/60 p-3 font-mono text-[11.5px]">{d.error ?? d.decision}</pre>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      </div>
    </Dialog>
  );
}
