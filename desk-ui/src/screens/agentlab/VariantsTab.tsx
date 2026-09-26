import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Play, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  blankVariant,
  getCatalog,
  getVariants,
  previewEdits,
  saveVariant,
  type Edit,
  type EditKind,
  type Variant,
} from "../../agentlab";
import { cn } from "../../cn";
import { useToast } from "../../components/toast";
import { Button, Card, ErrorNote, Label, Pill } from "../../components/ui";
import { Messages } from "./Messages";

const VENDORS = ["yfinance", "alpha_vantage", "sec_edgar", "fred", "polymarket"];
const KIND_HELP: Record<EditKind, string> = {
  append: "Adds a block to the end of the agent's instructions",
  prepend: "Adds a block before the agent's instructions",
  replace: "Replaces an exact passage wherever it appears in what the agent is sent",
};

const input = "w-full rounded-xl border border-rule bg-ground px-3 py-2.5 text-[14px] outline-none focus:border-accent";

function copyOf(v: Variant): Variant {
  return JSON.parse(JSON.stringify(v));
}

export function VariantsTab({ focus, onRun }: { focus: { agent?: string; variant?: string }; onRun: (v: string) => void }) {
  const vs = useQuery({ queryKey: ["agentlab", "variants"], queryFn: getVariants });
  const cat = useQuery({ queryKey: ["agentlab", "catalog"], queryFn: getCatalog });
  const qc = useQueryClient();
  const toast = useToast();
  const [selected, setSelected] = useState<string>(focus.variant ?? "baseline");
  const [draft, setDraft] = useState<Variant | null>(null);
  const [previewAgent, setPreviewAgent] = useState(focus.agent ?? "Portfolio Manager");
  const [busy, setBusy] = useState(false);

  // Arriving from an agent's "Edit in a variant": start a new variant with an edit for it.
  useEffect(() => {
    if (focus.agent) {
      const v = blankVariant();
      v.edits = [{ agent: focus.agent, kind: "append", text: "", find: "" }];
      setDraft(v);
      setSelected("");
      setPreviewAgent(focus.agent);
    }
  }, [focus.agent]);

  const saved = vs.data?.variants.find((v) => v.name === selected);
  const editing = draft ?? (saved ? copyOf(saved) : null);
  const isBaseline = !draft && selected === "baseline";
  const agents = cat.data?.agents.map((a) => a.node) ?? [];

  const update = (fn: (v: Variant) => void) => {
    const next = copyOf(editing!);
    fn(next);
    setDraft(next);
  };

  // Live preview of the unsaved variant against the latest captured prompt.
  const previewKey = useMemo(() => JSON.stringify(editing?.edits ?? []), [editing?.edits]);
  const preview = useQuery({
    queryKey: ["agentlab", "preview", previewAgent, previewKey],
    queryFn: () => previewEdits({ name: editing?.name || "draft", edits: editing?.edits ?? [] }, previewAgent),
    enabled: !!editing,
  });

  async function save() {
    if (!editing) return;
    setBusy(true);
    try {
      const v = await saveVariant({
        name: editing.name,
        description: editing.description,
        edits: editing.edits,
        models: editing.models,
        settings: editing.settings,
        vendors: editing.vendors,
        extra_tools: editing.extra_tools,
      });
      toast(`Saved ${v.name}, version ${v.version}.`);
      setDraft(null);
      setSelected(v.name);
      qc.invalidateQueries({ queryKey: ["agentlab", "variants"] });
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setBusy(false);
    }
  }

  if (vs.error) return <ErrorNote error={vs.error} />;
  if (!vs.data || !cat.data) return <div className="h-64 animate-pulse rounded-2xl bg-sunk" />;

  return (
    <div className="grid gap-5 lg:grid-cols-[210px_1fr]">
      <div className="space-y-2">
        <Button
          tone="primary"
          className="w-full"
          onClick={() => {
            setDraft(blankVariant());
            setSelected("");
          }}
        >
          <Plus size={16} /> New variant
        </Button>
        <ul className="space-y-0.5">
          {vs.data.variants.map((v) => (
            <li key={v.name}>
              <button
                type="button"
                onClick={() => {
                  setDraft(null);
                  setSelected(v.name);
                }}
                className={cn(
                  "w-full rounded-lg px-2.5 py-2 text-left",
                  !draft && selected === v.name ? "bg-accent-soft" : "hover:bg-sunk",
                )}
              >
                <span className={cn("block text-[14px] font-semibold", !draft && selected === v.name && "text-accent")}>{v.name}</span>
                <span className="block text-[12px] text-muted">
                  {v.name === "baseline" ? "production, unchanged" : `v${v.version} · ${v.edits.length} edit${v.edits.length === 1 ? "" : "s"}`}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {editing ? (
        <div className="grid min-w-0 gap-5 xl:grid-cols-[1fr_1fr]">
          <div className="min-w-0 space-y-4">
            <Card>
              {isBaseline ? (
                <div className="space-y-3">
                  <p className="text-[14px] text-muted">
                    Baseline is the production agents exactly as they run tonight. Duplicate it to change anything.
                  </p>
                  <Button
                    onClick={() => {
                      const v = blankVariant();
                      setDraft(v);
                      setSelected("");
                    }}
                  >
                    <Copy size={16} /> Start a variant from baseline
                  </Button>
                </div>
              ) : (
                <div className="space-y-3">
                  <div>
                    <label htmlFor="v-name" className="text-[13px] text-muted">Name</label>
                    <input
                      id="v-name"
                      className={cn(input, "num mt-1")}
                      value={editing.name}
                      disabled={!draft || !!saved}
                      placeholder="e.g. flat-book"
                      onChange={(e) => update((v) => void (v.name = e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-")))}
                    />
                  </div>
                  <div>
                    <label htmlFor="v-desc" className="text-[13px] text-muted">What it changes, and why</label>
                    <textarea
                      id="v-desc"
                      className={cn(input, "mt-1 min-h-16")}
                      value={editing.description}
                      onChange={(e) => update((v) => void (v.description = e.target.value))}
                    />
                  </div>
                </div>
              )}
            </Card>

            {!isBaseline ? (
              <>
                <Card>
                  <div className="flex items-center justify-between">
                    <Label>Prompt edits</Label>
                    <Button
                      className="min-h-9 px-3 text-[13px]"
                      onClick={() => update((v) => void v.edits.push({ agent: previewAgent, kind: "append", text: "", find: "" }))}
                    >
                      <Plus size={14} /> Add edit
                    </Button>
                  </div>
                  {editing.edits.length === 0 ? (
                    <p className="mt-2 text-[14px] text-muted">No prompt edits: this variant changes only the settings below.</p>
                  ) : null}
                  <div className="mt-3 space-y-3">
                    {editing.edits.map((e, i) => (
                      <EditCard
                        key={i}
                        e={e}
                        i={i}
                        agents={agents}
                        missed={preview.data?.node === e.agent && preview.data?.missed?.includes(i)}
                        onChange={(next) => update((v) => void (v.edits[i] = next))}
                        onRemove={() => update((v) => void v.edits.splice(i, 1))}
                        onFocus={() => setPreviewAgent(e.agent === "*" ? previewAgent : e.agent)}
                      />
                    ))}
                  </div>
                </Card>

                <Card>
                  <Label>Models</Label>
                  <div className="mt-2 grid gap-3 sm:grid-cols-2">
                    {(["deep", "quick"] as const).map((tier) => (
                      <div key={tier}>
                        <label htmlFor={`m-${tier}`} className="text-[13px] text-muted">
                          {tier === "deep" ? "Deep (research manager, portfolio manager)" : "Quick (everyone else)"}
                        </label>
                        <select
                          id={`m-${tier}`}
                          className={cn(input, "mt-1")}
                          value={editing.models[tier] ?? ""}
                          onChange={(ev) =>
                            update((v) => {
                              if (ev.target.value) v.models[tier] = ev.target.value;
                              else delete v.models[tier];
                            })
                          }
                        >
                          <option value="">production ({cat.data.models[tier]})</option>
                          {vs.data.models.map((m) => (
                            <option key={m} value={m}>
                              {m}
                            </option>
                          ))}
                        </select>
                      </div>
                    ))}
                  </div>
                </Card>

                <Card>
                  <Label>Settings</Label>
                  <div className="mt-2 grid gap-3 sm:grid-cols-3">
                    {(["max_debate_rounds", "max_risk_discuss_rounds", "holding_period_days"] as const).map((k) => (
                      <div key={k}>
                        <label htmlFor={`s-${k}`} className="text-[13px] text-muted">
                          {k.replaceAll("_", " ")}
                        </label>
                        <input
                          id={`s-${k}`}
                          type="number"
                          min={1}
                          max={60}
                          className={cn(input, "num mt-1")}
                          placeholder={String(cat.data.settings[k] ?? "")}
                          value={(editing.settings[k] as number | undefined) ?? ""}
                          onChange={(ev) =>
                            update((v) => {
                              if (ev.target.value) v.settings[k] = Number(ev.target.value);
                              else delete v.settings[k];
                            })
                          }
                        />
                      </div>
                    ))}
                  </div>
                  <label className="mt-3 flex items-center gap-2 text-[14px]">
                    <input
                      type="checkbox"
                      checked={(editing.settings.holding_period_framing ?? cat.data.settings.holding_period_framing) !== false}
                      onChange={(ev) => update((v) => void (v.settings.holding_period_framing = ev.target.checked))}
                    />
                    Tell no-mandate agents their holding period
                  </label>
                </Card>

                <Card>
                  <Label>Data vendors</Label>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2">
                    {Object.entries(cat.data.vendors).map(([catName, prod]) => (
                      <div key={catName} className="flex items-center justify-between gap-2">
                        <label htmlFor={`vd-${catName}`} className="text-[13px] text-muted">
                          {catName.replaceAll("_", " ")}
                        </label>
                        <select
                          id={`vd-${catName}`}
                          className="rounded-lg border border-rule bg-ground px-2 py-1.5 text-[13px]"
                          value={editing.vendors[catName] ?? ""}
                          onChange={(ev) =>
                            update((v) => {
                              if (ev.target.value) v.vendors[catName] = ev.target.value;
                              else delete v.vendors[catName];
                            })
                          }
                        >
                          <option value="">production ({prod})</option>
                          {VENDORS.map((x) => (
                            <option key={x} value={x}>
                              {x}
                            </option>
                          ))}
                        </select>
                      </div>
                    ))}
                  </div>
                </Card>

                <Card>
                  <Label>Extra tools for an analyst</Label>
                  <p className="mt-1 text-[13px] text-muted">Give an analyst a tool it doesn't have today, such as the harvested ownership data.</p>
                  <div className="mt-2 space-y-3">
                    {vs.data.analysts.map((key) => (
                      <div key={key}>
                        <div className="text-[13px] font-semibold capitalize">{key}</div>
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          {cat.data.tools
                            .filter((t) => !cat.data.agents.find((a) => a.role === key)?.tools.includes(t.name))
                            .map((t) => {
                              const on = (editing.extra_tools[key] ?? []).includes(t.name);
                              return (
                                <button
                                  key={t.name}
                                  type="button"
                                  onClick={() =>
                                    update((v) => {
                                      const cur = new Set(v.extra_tools[key] ?? []);
                                      if (on) cur.delete(t.name);
                                      else cur.add(t.name);
                                      if (cur.size) v.extra_tools[key] = [...cur];
                                      else delete v.extra_tools[key];
                                    })
                                  }
                                  className={cn(
                                    "num rounded-full border px-2.5 py-1 text-[11.5px]",
                                    on ? "border-accent bg-accent-soft text-accent" : "border-rule text-muted hover:text-ink",
                                  )}
                                >
                                  {t.name}
                                </button>
                              );
                            })}
                        </div>
                      </div>
                    ))}
                  </div>
                </Card>

                <div className="flex flex-wrap gap-2">
                  <Button tone="primary" disabled={busy || !editing.name} onClick={save}>
                    {busy ? "Saving…" : saved && !draft ? "Saved" : "Save variant"}
                  </Button>
                  {saved && !draft ? (
                    <Button onClick={() => onRun(saved.name)}>
                      <Play size={16} /> Test it
                    </Button>
                  ) : null}
                  {draft && saved ? <Button onClick={() => setDraft(null)}>Discard changes</Button> : null}
                </div>
              </>
            ) : null}
          </div>

          <div className="min-w-0">
            <Card className="xl:sticky xl:top-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Label>What the agent would be sent</Label>
                <select
                  aria-label="Agent to preview"
                  className="rounded-lg border border-rule bg-ground px-2 py-1.5 text-[13px]"
                  value={previewAgent}
                  onChange={(ev) => setPreviewAgent(ev.target.value)}
                >
                  {agents.map((a) => (
                    <option key={a}>{a}</option>
                  ))}
                </select>
              </div>
              {preview.data?.missed?.length ? (
                <p className="mt-2 rounded-lg bg-warn-soft px-3 py-2 text-[13px] text-warn">
                  {preview.data.missed.length} replace edit{preview.data.missed.length === 1 ? "" : "s"} did not find its passage in this
                  prompt, so it would change nothing.
                </p>
              ) : null}
              {preview.data?.capture && preview.data.after ? (
                <>
                  <p className="mt-1 text-[12px] text-faint">
                    From {preview.data.capture.ticker} on {preview.data.capture.date}; highlighted text is what this variant adds.
                  </p>
                  <Messages
                    messages={preview.data.after}
                    highlight={editing.edits.filter((e) => e.agent === previewAgent || e.agent === "*").map((e) => e.text)}
                  />
                </>
              ) : (
                <p className="mt-2 text-[14px] text-muted">
                  {preview.data?.note ?? "Loading…"}
                </p>
              )}
            </Card>
          </div>
        </div>
      ) : (
        <Card>
          <p className="text-[14px] text-muted">Pick a variant, or start a new one.</p>
        </Card>
      )}
    </div>
  );
}

function EditCard({
  e,
  i,
  agents,
  missed,
  onChange,
  onRemove,
  onFocus,
}: {
  e: Edit;
  i: number;
  agents: string[];
  missed?: boolean;
  onChange: (e: Edit) => void;
  onRemove: () => void;
  onFocus: () => void;
}) {
  return (
    <div className={cn("space-y-2 rounded-xl border p-3", missed ? "border-warn" : "border-rule")} onFocus={onFocus}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="num text-[12px] text-faint">#{i + 1}</span>
        <select
          aria-label={`Edit ${i + 1} agent`}
          className="rounded-lg border border-rule bg-ground px-2 py-1.5 text-[13px]"
          value={e.agent}
          onChange={(ev) => onChange({ ...e, agent: ev.target.value })}
        >
          <option value="*">Every agent</option>
          {agents.map((a) => (
            <option key={a}>{a}</option>
          ))}
        </select>
        <div className="inline-flex overflow-hidden rounded-lg border border-rule">
          {(["append", "prepend", "replace"] as const).map((k) => (
            <button
              key={k}
              type="button"
              title={KIND_HELP[k]}
              onClick={() => onChange({ ...e, kind: k })}
              className={cn("px-2.5 py-1.5 text-[12.5px]", e.kind === k ? "bg-accent-soft font-semibold text-accent" : "text-muted")}
            >
              {k}
            </button>
          ))}
        </div>
        {missed ? <Pill tone="warn">passage not found</Pill> : null}
        <button type="button" aria-label={`Remove edit ${i + 1}`} onClick={onRemove} className="ml-auto rounded-lg p-1.5 text-faint hover:bg-sunk hover:text-halt">
          <Trash2 size={16} />
        </button>
      </div>
      {e.kind === "replace" ? (
        <textarea
          aria-label={`Edit ${i + 1}: passage to replace`}
          className={cn(input, "num min-h-16 text-[12.5px]")}
          placeholder="The exact passage to replace (copy it from the prompt on the right)"
          value={e.find}
          onChange={(ev) => onChange({ ...e, find: ev.target.value })}
        />
      ) : null}
      <textarea
        aria-label={`Edit ${i + 1}: text`}
        className={cn(input, "min-h-24 text-[13px]")}
        placeholder={e.kind === "replace" ? "Replace it with…" : "The instructions to add"}
        value={e.text}
        onChange={(ev) => onChange({ ...e, text: ev.target.value })}
      />
    </div>
  );
}
