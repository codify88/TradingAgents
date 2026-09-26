import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Pencil, Wrench } from "lucide-react";
import { useState } from "react";
import { getCatalog, previewEdits, STAGES, type ToolInfo } from "../../agentlab";
import { cn } from "../../cn";
import { Button, Card, ErrorNote, Label, Pill } from "../../components/ui";
import { Messages } from "./Messages";

function ToolCard({ t }: { t: ToolInfo }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl border border-rule bg-ground/60">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-start justify-between gap-3 p-3 text-left">
        <span className="min-w-0">
          <span className="num block text-[13px] font-medium">{t.name}</span>
          <span className="mt-0.5 block text-[13px] text-muted">{t.description}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2">
          {t.vendor ? <Pill tone={t.vendor === "alpha_vantage" ? "accent" : "neutral"}>{t.vendor}</Pill> : null}
          <ChevronDown size={16} className={cn("text-faint transition-transform", open && "rotate-180")} />
        </span>
      </button>
      {open ? (
        <dl className="border-t border-rule px-3 py-2 text-[12.5px]">
          {Object.entries(t.args).map(([k, v]) => (
            <div key={k} className="flex gap-3 py-1">
              <dt className="num w-32 shrink-0 text-muted">{k}</dt>
              <dd className="text-ink">{v}</dd>
            </div>
          ))}
          <div className="flex gap-3 py-1">
            <dt className="w-32 shrink-0 text-muted">category</dt>
            <dd>{t.category ?? "—"}</dd>
          </div>
        </dl>
      ) : null}
    </div>
  );
}

export function AgentsTab({ onEdit }: { onEdit: (agent: string) => void }) {
  const { data, error } = useQuery({ queryKey: ["agentlab", "catalog"], queryFn: getCatalog });
  const [node, setNode] = useState("Portfolio Manager");
  const sent = useQuery({
    queryKey: ["agentlab", "sent", node],
    queryFn: () => previewEdits("baseline", node),
  });
  if (error) return <ErrorNote error={error} />;
  if (!data) return <div className="h-64 animate-pulse rounded-2xl bg-sunk" />;
  const agent = data.agents.find((a) => a.node === node)!;
  const tools = data.tools.filter((t) => agent.tools.includes(t.name));

  return (
    <div className="grid gap-5 md:grid-cols-[230px_1fr]">
      <nav aria-label="Agents" className="space-y-4">
        {STAGES.map((stage) => (
          <div key={stage.label}>
            <Label>{stage.label}</Label>
            <ul className="mt-1.5 space-y-0.5">
              {stage.nodes.map((n) => {
                const a = data.agents.find((x) => x.node === n);
                return (
                  <li key={n}>
                    <button
                      type="button"
                      onClick={() => setNode(n)}
                      className={cn(
                        "flex w-full items-center justify-between rounded-lg px-2.5 py-1.5 text-left text-[14px]",
                        node === n ? "bg-accent-soft font-semibold text-accent" : "text-muted hover:bg-sunk hover:text-ink",
                      )}
                    >
                      {n}
                      {a?.tier === "deep" ? <span className="text-[10px] font-semibold uppercase tracking-wide text-faint">deep</span> : null}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="min-w-0 space-y-4">
        <Card>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-[20px] font-bold tracking-tight">{agent.node}</h2>
              <p className="mt-1 text-[14px] text-muted">Reads {agent.reads}.</p>
            </div>
            <Button tone="primary" onClick={() => onEdit(agent.node)}>
              <Pencil size={16} /> Edit in a variant
            </Button>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <Pill tone="accent">{agent.tier} model</Pill>
            <Pill className="num">{agent.model}</Pill>
          </div>
        </Card>

        <Card>
          <Label>Tools it can call</Label>
          {tools.length ? (
            <div className="mt-2 space-y-2">
              {tools.map((t) => (
                <ToolCard key={t.name} t={t} />
              ))}
            </div>
          ) : (
            <p className="mt-2 text-[14px] text-muted">
              None: it works from what the earlier agents wrote. Its only levers are its prompt and model.
            </p>
          )}
        </Card>

        <Card>
          <div className="flex items-baseline justify-between gap-3">
            <Label>What it was sent</Label>
            {sent.data?.capture ? (
              <span className="text-[12px] text-faint">
                {sent.data.capture.ticker} · {sent.data.capture.date} · {sent.data.capture.model}
              </span>
            ) : null}
          </div>
          {sent.data?.capture && sent.data.before ? (
            <Messages messages={sent.data.before} />
          ) : (
            <p className="mt-2 text-[14px] text-muted">
              {sent.data?.note ??
                "Loading…"}{" "}
              The first run of any variant captures every agent's prompt.
            </p>
          )}
        </Card>

        <Card>
          <div className="flex items-center gap-2">
            <Wrench size={16} className="text-muted" />
            <Label>Data and tools across all agents</Label>
          </div>
          <div className="mt-3 grid gap-4 md:grid-cols-2">
            <div>
              <div className="text-[13px] font-semibold">Vendors in use</div>
              <dl className="mt-1.5 space-y-1 text-[13px]">
                {Object.entries(data.vendors).map(([cat, v]) => (
                  <div key={cat} className="flex justify-between gap-3">
                    <dt className="text-muted">{cat.replaceAll("_", " ")}</dt>
                    <dd className="num">{v}</dd>
                  </div>
                ))}
              </dl>
            </div>
            <div>
              <div className="text-[13px] font-semibold">Defined, but no agent calls them</div>
              <p className="mt-1 text-[12.5px] text-muted">Harvested data the agents never see. A variant can add them to an analyst.</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {data.unused_tools.map((t) => (
                  <Pill key={t} tone="warn" className="num">
                    {t}
                  </Pill>
                ))}
              </div>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
