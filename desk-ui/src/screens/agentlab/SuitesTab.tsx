import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { buildSuite, getSuites } from "../../agentlab";
import { useToast } from "../../components/toast";
import { Button, Card, Empty, ErrorNote, Label } from "../../components/ui";

const input = "mt-1 w-full rounded-xl border border-rule bg-ground px-3 py-2.5 text-[14px] outline-none focus:border-accent";

export function SuitesTab() {
  const { data, error } = useQuery({ queryKey: ["agentlab", "suites"], queryFn: getSuites });
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({ name: "", start: "2025-08-01", end: "2026-08-28", count: 40, picks: 8, controls: 4 });
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form, v: string) => setForm({ ...form, [k]: ["count", "picks", "controls"].includes(k) ? Number(v) : v });

  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_360px]">
      <Card>
        <Label>Suites</Label>
        {error ? <ErrorNote error={error} /> : null}
        {data && data.suites.length === 0 ? <Empty title="No suites yet">Build one on the right.</Empty> : null}
        <ul className="mt-2 divide-y divide-rule">
          {data?.suites.map((s) => (
            <li key={s.name} className="py-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-[15px] font-semibold">{s.name}</span>
                <span className="num text-[13px] text-muted">
                  {s.cases} cases · {s.dates.length} date{s.dates.length === 1 ? "" : "s"}
                </span>
              </div>
              <p className="text-[13px] text-muted">{s.description}</p>
              <p className="num text-[12px] text-faint">
                {s.dates[0]}
                {s.dates.length > 1 ? ` … ${s.dates[s.dates.length - 1]}` : ""} · {s.picks} picks + {s.controls} controls a date
              </p>
            </li>
          ))}
        </ul>
        {data ? (
          <div className="mt-4 rounded-xl bg-sunk/60 p-3 text-[12.5px] text-muted">
            A model can't be tested on weeks it may have read about. Training-data cutoffs:{" "}
            {Object.entries(data.cutoffs)
              .map(([m, d]) => `${m} ${d}`)
              .join(" · ")}
            .
          </div>
        ) : null}
      </Card>

      <Card>
        <Label>Build a suite</Label>
        <p className="mt-1 text-[13px] text-muted">
          The standard screen on past Fridays, spread evenly: its picks and random controls. Costs price requests only, no model calls.
        </p>
        <form
          className="mt-3 space-y-3"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            try {
              toast((await buildSuite(form)).result);
              qc.invalidateQueries({ queryKey: ["agentlab", "suites"] });
            } catch (err) {
              toast(err instanceof Error ? err.message : String(err), "error");
            } finally {
              setBusy(false);
            }
          }}
        >
          <div>
            <label htmlFor="su-name" className="text-[13px] text-muted">Name</label>
            <input id="su-name" className={`${input} num`} placeholder="e.g. weekly-2025h2" value={form.name} onChange={(e) => set("name", e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="su-start" className="text-[13px] text-muted">From</label>
              <input id="su-start" type="date" className={input} value={form.start} onChange={(e) => set("start", e.target.value)} />
            </div>
            <div>
              <label htmlFor="su-end" className="text-[13px] text-muted">To</label>
              <input id="su-end" type="date" className={input} value={form.end} onChange={(e) => set("end", e.target.value)} />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            {(["count", "picks", "controls"] as const).map((k) => (
              <div key={k}>
                <label htmlFor={`su-${k}`} className="text-[13px] text-muted">{k === "count" ? "Dates" : k}</label>
                <input id={`su-${k}`} type="number" min={0} max={100} className={`${input} num`} value={form[k]} onChange={(e) => set(k, e.target.value)} />
              </div>
            ))}
          </div>
          <p className="num text-[12.5px] text-faint">= {form.count * (form.picks + form.controls)} decisions per run</p>
          <Button tone="primary" type="submit" disabled={busy || !form.name} className="w-full">
            {busy ? "Starting…" : "Build suite"}
          </Button>
        </form>
      </Card>
    </div>
  );
}
