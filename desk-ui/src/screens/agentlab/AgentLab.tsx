import { useState } from "react";
import { cn } from "../../cn";
import { AgentsTab } from "./AgentsTab";
import { RunsTab } from "./RunsTab";
import { SuitesTab } from "./SuitesTab";
import { VariantsTab } from "./VariantsTab";

const TABS = [
  { key: "agents", label: "Agents", blurb: "What each agent reads, calls, and was sent" },
  { key: "variants", label: "Variants", blurb: "Named prompt edits, models, data and tools" },
  { key: "runs", label: "Runs", blurb: "A variant on a suite, and what it measured" },
  { key: "suites", label: "Suites", blurb: "Fixed historical cases to test on" },
] as const;
type Tab = (typeof TABS)[number]["key"];

function initialTab(): Tab {
  const h = window.location.hash.slice(1);
  return (TABS.find((t) => t.key === h)?.key ?? "agents") as Tab;
}

export function AgentLabScreen() {
  const [tab, setTab] = useState<Tab>(initialTab);
  // A variant chosen elsewhere (e.g. "Edit in a variant" on an agent) opens here.
  const [focus, setFocus] = useState<{ agent?: string; variant?: string }>({});

  const go = (t: Tab, f: { agent?: string; variant?: string } = {}) => {
    setFocus(f);
    setTab(t);
    history.replaceState(null, "", `#${t}`);
  };

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-[26px] font-bold tracking-tight">Agent lab</h1>
        <p className="text-[14px] text-muted">
          See what the agents are told and given, change it as a named variant, and test it on fixed past cases,
          the way the screen lab tests screens.
        </p>
      </div>
      <div role="tablist" className="flex gap-1 overflow-x-auto border-b border-rule">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            type="button"
            onClick={() => go(t.key)}
            className={cn(
              "-mb-px shrink-0 border-b-2 px-3 py-2.5 text-left",
              tab === t.key ? "border-accent text-ink" : "border-transparent text-muted hover:text-ink",
            )}
          >
            <span className="block text-[14px] font-semibold">{t.label}</span>
            <span className="hidden text-[12px] text-faint md:block">{t.blurb}</span>
          </button>
        ))}
      </div>
      {tab === "agents" ? <AgentsTab onEdit={(agent) => go("variants", { agent })} /> : null}
      {tab === "variants" ? <VariantsTab focus={focus} onRun={(variant) => go("runs", { variant })} /> : null}
      {tab === "runs" ? <RunsTab focus={focus} /> : null}
      {tab === "suites" ? <SuitesTab /> : null}
    </div>
  );
}
