import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { getCatalog, getKnobs, getVariants, saveKnobVariant } from "../../agentlab";
import { cn } from "../../cn";
import { useToast } from "../../components/toast";
import { Button, Card, ErrorNote, Label, Pill } from "../../components/ui";

const input = "mt-1 w-full rounded-xl border border-rule bg-ground px-3 py-2.5 text-[14px] outline-none focus:border-accent";

export function KnobsTab({ onReplay }: { onReplay: (variant: string) => void }) {
  const knobs = useQuery({ queryKey: ["agentlab", "knobs"], queryFn: getKnobs, staleTime: Infinity });
  const vs = useQuery({ queryKey: ["agentlab", "variants"], queryFn: getVariants });
  const cat = useQuery({ queryKey: ["agentlab", "catalog"], queryFn: getCatalog, staleTime: Infinity });
  const qc = useQueryClient();
  const toast = useToast();
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [name, setName] = useState("");
  const [deep, setDeep] = useState("");
  const [quick, setQuick] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  const production = knobs.data?.production ?? {};
  const picked = { ...production, ...choices };
  const changed = useMemo(
    () => Object.entries(picked).filter(([k, v]) => production[k] !== v),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [choices, knobs.data],
  );
  const knobVariants = vs.data?.variants.filter((v) => v.knobs && Object.keys(v.knobs).length) ?? [];

  async function save() {
    setBusy(true);
    try {
      const v = await saveKnobVariant({
        name,
        choices: Object.fromEntries(changed),
        models: { ...(deep ? { deep } : {}), ...(quick ? { quick } : {}) },
      });
      toast(`Saved ${v.name} (v${v.version})`);
      setSaved(v.name);
      qc.invalidateQueries({ queryKey: ["agentlab", "variants"] });
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy(false);
    }
  }

  function load(k: Record<string, string>, n: string, models: { deep?: string; quick?: string }) {
    setChoices(k);
    setName(n);
    setDeep(models.deep ?? "");
    setQuick(models.quick ?? "");
    setSaved(null);
  }

  if (knobs.error) return <ErrorNote error={knobs.error} />;
  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_340px]">
      <div className="space-y-4">
        <p className="text-[13.5px] text-muted">
          Each knob is one change aimed at the Hold habit. The first option is production, so a knob left alone changes
          nothing. Turn one at a time, save it as a variant, and replay it against baseline on the same cases. The
          Playbook tab has the order.
        </p>
        {knobs.data?.knobs.map((k) => (
          <Card key={k.key}>
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-[15px] font-semibold">{k.label}</span>
              <span className="text-[12.5px] text-faint">{k.question}</span>
            </div>
            <div role="radiogroup" aria-label={k.label} className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {k.options.map((o, i) => {
                const on = picked[k.key] === o.key;
                return (
                  <button
                    key={o.key}
                    type="button"
                    role="radio"
                    aria-checked={on}
                    onClick={() => {
                      setChoices({ ...choices, [k.key]: o.key });
                      setSaved(null);
                    }}
                    className={cn(
                      "rounded-xl border px-3 py-2.5 text-left transition-colors",
                      on ? "border-accent bg-accent-soft/40" : "border-rule hover:bg-sunk",
                    )}
                  >
                    <span className="flex items-center gap-2 text-[14px] font-medium">
                      {o.label}
                      {i === 0 ? <Pill>production</Pill> : null}
                    </span>
                    <span className="mt-0.5 block text-[12.5px] leading-snug text-muted">{o.detail}</span>
                    {o.agents.length || Object.keys(o.settings).length ? (
                      <span className="mt-1 block font-mono text-[10.5px] text-faint">
                        {[...o.agents.map((a) => (a === "*" ? "every agent" : a)), ...Object.keys(o.settings)].join(" · ")}
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </Card>
        ))}
      </div>

      <div className="space-y-4 lg:sticky lg:top-4 lg:self-start">
        <Card>
          <Label>This combination</Label>
          {changed.length ? (
            <ul className="mt-2 space-y-1 text-[13.5px]">
              {changed.map(([k, v]) => {
                const knob = knobs.data?.knobs.find((x) => x.key === k);
                return (
                  <li key={k}>
                    <span className="text-muted">{knob?.label}: </span>
                    <span className="font-medium">{knob?.options.find((o) => o.key === v)?.label}</span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="mt-2 text-[13.5px] text-faint">Production: every knob at its first option. Replay "baseline" for this.</p>
          )}
          <div className="mt-4 space-y-3">
            <div>
              <label htmlFor="kn-name" className="text-[13px] text-muted">Variant name</label>
              <input
                id="kn-name"
                className={`${input} num`}
                placeholder="e.g. entry-scale"
                value={name}
                onChange={(e) => {
                  setName(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"));
                  setSaved(null);
                }}
              />
            </div>
            <div className="grid grid-cols-2 gap-2">
              {(
                [
                  ["deep", deep, setDeep],
                  ["quick", quick, setQuick],
                ] as const
              ).map(([k, v, set]) => (
                <div key={k}>
                  <label htmlFor={`kn-${k}`} className="text-[12.5px] text-muted">{k === "deep" ? "Deep model" : "Quick model"}</label>
                  <select id={`kn-${k}`} className={input} value={v} onChange={(e) => set(e.target.value)}>
                    <option value="">Production ({cat.data?.models[k] ?? "…"})</option>
                    {vs.data?.models.map((m) => (
                      <option key={m} value={m}>{m}</option>
                    ))}
                  </select>
                </div>
              ))}
            </div>
            <p className="text-[12px] text-faint">
              Haiku for a first look at the rating mix is cents a case; re-run leaders on production models, since models
              differ in exactly this habit.
            </p>
            <Button tone="primary" className="w-full" disabled={busy || !name || !changed.length} onClick={save}>
              {busy ? "Saving…" : "Save as variant"}
            </Button>
            {saved ? (
              <Button className="w-full" onClick={() => onReplay(saved)}>
                Replay {saved} →
              </Button>
            ) : null}
          </div>
        </Card>

        {knobVariants.length ? (
          <Card>
            <Label>Saved knob variants</Label>
            <ul className="mt-2 divide-y divide-rule">
              {knobVariants.map((v) => (
                <li key={v.name} className="flex items-center gap-2 py-2">
                  <button type="button" className="min-w-0 flex-1 text-left" onClick={() => load(v.knobs ?? {}, v.name, v.models)}>
                    <span className="block text-[14px] font-medium">
                      {v.name} <span className="text-faint">v{v.version}</span>
                    </span>
                    <span className="block truncate text-[12px] text-faint">{v.description.replace(/^Knobs -- /, "")}</span>
                  </button>
                  <Button className="min-h-8 px-2.5 text-[12.5px]" onClick={() => onReplay(v.name)}>
                    Replay
                  </Button>
                </li>
              ))}
            </ul>
          </Card>
        ) : null}
      </div>
    </div>
  );
}
