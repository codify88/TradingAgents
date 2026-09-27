import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { estimateRun, getJobs, getRuns, getSuites, getVariants, startRun, type Estimate, type RunMetrics } from "../../agentlab";
import { cn } from "../../cn";
import { useToast } from "../../components/toast";
import { Button, Card, Empty, ErrorNote, Label, Pill } from "../../components/ui";

const RATING_TONE: Record<string, string> = {
  Buy: "bg-gain",
  Overweight: "bg-gain/60",
  Hold: "bg-faint/50",
  Underweight: "bg-loss/60",
  Sell: "bg-loss",
};

const pp = (x: number | null | undefined, digits = 1) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`);
const usd = (x: number | null | undefined) => (x == null ? "—" : `$${x.toFixed(x < 10 ? 2 : 0)}`);

function RatingBar({ r }: { r: Record<string, number> }) {
  const total = Object.values(r).reduce((a, b) => a + b, 0);
  if (!total) return <span className="text-faint">—</span>;
  return (
    <div className="flex h-2.5 w-28 overflow-hidden rounded-full bg-sunk" title={Object.entries(r).map(([k, v]) => `${k} ${v}`).join(", ")}>
      {Object.entries(r).map(([k, v]) =>
        v ? <span key={k} className={RATING_TONE[k]} style={{ width: `${(v / total) * 100}%` }} /> : null,
      )}
    </div>
  );
}

export function RunsTab({ focus }: { focus: { variant?: string } }) {
  const vs = useQuery({ queryKey: ["agentlab", "variants"], queryFn: getVariants });
  const ss = useQuery({ queryKey: ["agentlab", "suites"], queryFn: getSuites });
  const rs = useQuery({ queryKey: ["agentlab", "runs"], queryFn: getRuns, refetchInterval: 20_000 });
  const jobs = useQuery({ queryKey: ["agentlab", "jobs"], queryFn: getJobs, refetchInterval: 20_000 });
  const qc = useQueryClient();
  const toast = useToast();
  const [variant, setVariant] = useState(focus.variant ?? "baseline");
  const [suite, setSuite] = useState("");
  const [est, setEst] = useState<Estimate | null>(null);
  const [agree, setAgree] = useState(false);
  const [overCap, setOverCap] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const ready = ss.data?.suites.filter((s) => s.status === "ready") ?? [];
    if (!suite && ready.length) setSuite(ready[0].name);
  }, [ss.data, suite]);

  useEffect(() => {
    setEst(null);
    setAgree(false);
    setOverCap(false);
    if (!variant || !suite) return;
    estimateRun(variant, suite).then(setEst, () => setEst(null));
  }, [variant, suite]);

  async function launch() {
    if (!est?.cost_high) return;
    setBusy(true);
    try {
      const r = await startRun(variant, suite, est.cost_high, overCap);
      toast(r.result);
      qc.invalidateQueries({ queryKey: ["agentlab"] });
      setAgree(false);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy(false);
    }
  }

  const over = !!est?.cost_high && est.cost_high > est.max_cost;

  return (
    <div className="space-y-5">
      <Card>
        <Label>Test a variant</Label>
        <div className="mt-3 grid gap-3 md:grid-cols-[1fr_1fr_auto] md:items-end">
          <div>
            <label htmlFor="r-variant" className="text-[13px] text-muted">Variant</label>
            <select id="r-variant" className="mt-1 w-full rounded-xl border border-rule bg-ground px-3 py-2.5 text-[14px]" value={variant} onChange={(e) => setVariant(e.target.value)}>
              {vs.data?.variants.map((v) => (
                <option key={v.name} value={v.name}>
                  {v.name}
                  {v.version ? ` (v${v.version})` : ""}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="r-suite" className="text-[13px] text-muted">Suite</label>
            <select id="r-suite" className="mt-1 w-full rounded-xl border border-rule bg-ground px-3 py-2.5 text-[14px]" value={suite} onChange={(e) => setSuite(e.target.value)}>
              {ss.data?.suites.map((s) => (
                <option key={s.name} value={s.name} disabled={s.status !== "ready"}>
                  {s.name} · {s.status === "ready" ? `${s.cases} cases` : `${s.status} ${s.done}/${s.total}`}
                </option>
              ))}
            </select>
          </div>
        </div>
        {est ? (
          <div className="mt-4 space-y-3">
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-[14px]">
              <span>
                <span className="text-muted">Decisions </span>
                <span className="num font-medium">{est.cases}</span>
              </span>
              <span>
                <span className="text-muted">Models </span>
                <span className="num">{est.models.join(" + ")}</span>
              </span>
              <span>
                <span className="text-muted">Cost </span>
                <span className="num font-medium">
                  {est.cost_low === est.cost_high ? usd(est.cost_high) : `${usd(est.cost_low)}–${usd(est.cost_high)}`}
                </span>
                <span className="text-faint"> (API credits)</span>
              </span>
              <span>
                <span className="text-muted">Time </span>
                <span className="num">~{Math.round(est.minutes / 60)} h</span>
              </span>
            </div>
            {est.cutoff_warning ? <p className="rounded-lg bg-warn-soft px-3 py-2 text-[13px] text-warn">{est.cutoff_warning}.</p> : null}
            <label className="flex items-center gap-2 text-[14px]">
              <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} />
              Spend up to {usd(est.cost_high)} of API credits on this run
            </label>
            {over ? (
              <label className="flex items-center gap-2 text-[14px] text-warn">
                <input type="checkbox" checked={overCap} onChange={(e) => setOverCap(e.target.checked)} />
                That is over the ${est.max_cost.toFixed(0)} cap; go over it for this run
              </label>
            ) : null}
            <Button tone="primary" disabled={!agree || busy || (over && !overCap)} onClick={launch}>
              {busy ? "Starting…" : "Start the run"}
            </Button>
            <p className="text-[12px] text-faint">Runs start as background jobs, not between 01:30 and 07:30 or while another job runs.</p>
          </div>
        ) : (
          <p className="mt-3 text-[13px] text-faint">{ss.data?.suites.length ? "Estimating…" : "Build a suite first."}</p>
        )}
      </Card>

      <Card>
        <Label>Runs</Label>
        {rs.error ? <ErrorNote error={rs.error} /> : null}
        {rs.data && rs.data.runs.length === 0 ? <Empty title="No runs yet">Start one above.</Empty> : null}
        {rs.data?.runs.length ? (
          <div className="-mx-4 mt-2 overflow-x-auto px-4">
            <table className="w-full min-w-[860px] text-[13px]">
              <thead>
                <tr className="text-left font-mono text-[11px] uppercase tracking-wide text-faint">
                  <th className="py-2 pr-3">Variant</th>
                  <th className="pr-3">Suite</th>
                  <th className="pr-3">Decided</th>
                  <th className="pr-3">Ratings</th>
                  <th className="pr-3" title="Stated time horizon matches the holding period">Horizon</th>
                  <th className="pr-3" title="5-day alpha of Buy/Overweight names minus the rest">Agents</th>
                  <th className="pr-3" title="Bullish on names that beat the benchmark, bearish on names that lagged">Hit rate</th>
                  <th className="pr-3" title="The screen's own edge on these cases">Picks − controls</th>
                  <th className="pr-3">Cost</th>
                  <th>Edits</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-rule">
                {rs.data.runs.map((m) => (
                  <RunRow key={m.id} m={m} />
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        <p className="mt-3 text-[12px] text-faint">
          Small suites are for checking behaviour, not proof: the agents column needs dozens of dates before its t-stat means much.
        </p>
      </Card>

      {jobs.data ? (
        <Card>
          <Label>Background jobs</Label>
          <pre className="mt-2 whitespace-pre-wrap font-mono text-[12px] text-muted">{jobs.data.text}</pre>
        </Card>
      ) : null}
    </div>
  );
}

function RunRow({ m }: { m: RunMetrics }) {
  const missed = Object.values(m.edits.missed ?? {}).reduce((a, b) => a + b, 0);
  const applied = Object.values(m.edits.applied ?? {}).reduce((a, b) => a + b, 0);
  return (
    <tr className="align-top">
      <td className="py-2.5 pr-3">
        <span className="font-semibold">{m.variant}</span>
        {m.version ? <span className="text-faint"> v{m.version}</span> : null}
        <span className="block text-[11px] text-faint">{m.status === "running" ? "running" : m.finished?.slice(0, 16).replace("T", " ")}</span>
      </td>
      <td className="pr-3 text-muted">{m.suite}</td>
      <td className="num pr-3">
        {m.decided}
        {m.failures ? <span className="text-loss"> · {m.failures} failed</span> : null}
      </td>
      <td className="pr-3 pt-3">
        <RatingBar r={m.ratings} />
      </td>
      <td className="num pr-3">{m.horizon_stated ? `${Math.round((m.horizon_matched / m.horizon_stated) * 100)}%` : "—"}</td>
      <td className={cn("num pr-3", m.agents != null && (m.agents > 0 ? "text-gain" : "text-loss"))}>
        {pp(m.agents)}
        {m.agents_t != null ? <span className="block text-[11px] text-faint">t {m.agents_t.toFixed(1)} · {m.bullish_n} bullish</span> : null}
      </td>
      <td className="num pr-3">{m.hit_rate == null ? "—" : `${Math.round(m.hit_rate * 100)}%`}<span className="block text-[11px] text-faint">{m.calls} calls</span></td>
      <td className="num pr-3">{pp(m.picks_vs_controls)}</td>
      <td className="num pr-3">
        {usd(m.cost_low)}
        {m.per_decision != null ? <span className="block text-[11px] text-faint">{usd(m.per_decision)}/name</span> : null}
      </td>
      <td>{missed ? <Pill tone="warn">{missed} missed</Pill> : applied ? <Pill tone="gain">{applied} applied</Pill> : <span className="text-faint">—</span>}</td>
    </tr>
  );
}
