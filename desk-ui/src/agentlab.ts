import { get, post } from "./api";

// --- shapes from tradingagents/desk/agentlab_api.py ---------------------------------

export interface AgentInfo {
  node: string;
  role: string;
  tier: "deep" | "quick";
  model: string;
  reads: string;
  tools: string[];
}

export interface ToolInfo {
  name: string;
  description: string;
  args: Record<string, string>;
  category: string | null;
  vendor: string | null;
  used_by: string[];
}

export interface Catalog {
  agents: AgentInfo[];
  tools: ToolInfo[];
  unused_tools: string[];
  vendors: Record<string, string>;
  tool_vendors: Record<string, string>;
  models: { deep: string; quick: string; provider: string };
  settings: Record<string, number | boolean>;
}

export type EditKind = "append" | "prepend" | "replace";

export interface Edit {
  agent: string;
  kind: EditKind;
  text: string;
  find: string;
}

export interface Variant {
  name: string;
  description: string;
  edits: Edit[];
  models: { deep?: string; quick?: string };
  settings: Record<string, number | boolean>;
  vendors: Record<string, string>;
  extra_tools: Record<string, string[]>;
  version: number;
  saved: string;
}

export interface VariantsResponse {
  variants: Variant[];
  kinds: EditKind[];
  settings: string[];
  analysts: string[];
  models: string[];
}

export interface Message {
  role: string;
  text: string;
}

export interface Preview {
  node: string;
  capture: null | { run: string; ticker: string; date: string; model: string; at: string };
  note?: string;
  before?: Message[];
  after?: Message[];
  applied?: number[];
  missed?: number[];
}

export interface Suite {
  name: string;
  description: string;
  created: string;
  dates: string[];
  cases: number;
  picks: number;
  controls: number;
  status: "building" | "ready" | "failed";
  total: number;
  done: number;
  error: string;
}

export interface Estimate {
  suite: string;
  cases: number;
  dates: number;
  models: string[];
  cost_low: number | null;
  cost_high: number | null;
  minutes: number;
  cutoff_warning: string | null;
  max_cost: number;
}

export interface RunMetrics {
  id: string;
  variant: string;
  version: number;
  suite: string;
  status: string;
  started: string;
  finished: string | null;
  decided: number;
  settled: number;
  ratings: Record<string, number>;
  horizon_stated: number;
  horizon_matched: number;
  agents: number | null;
  agents_t: number | null;
  bullish_n: number;
  hit_rate: number | null;
  calls: number;
  picks_vs_controls: number | null;
  tokens_in: number;
  tokens_out: number;
  llm_calls: number;
  cost_low: number | null;
  cost_high: number | null;
  per_decision: number | null;
  edits: { applied?: Record<string, number>; missed?: Record<string, number> };
  failures: number;
  models: string[];
}

// --- calls -------------------------------------------------------------------------

export const getCatalog = () => get<Catalog>("/api/v1/agents/catalog");
export const getVariants = () => get<VariantsResponse>("/api/v1/agents/variants");
export const getSuites = () =>
  get<{ suites: Suite[]; cutoffs: Record<string, string>; seconds_per_date: number }>("/api/v1/agents/suites");
export const getRuns = () => get<{ runs: RunMetrics[] }>("/api/v1/agents/runs");
export const getJobs = () => get<{ text: string }>("/api/v1/agents/jobs");

export const saveVariant = (v: Partial<Variant>) => post<Variant>("/api/v1/agents/variants", v);
export const previewEdits = (variant: Partial<Variant> | string, node: string) =>
  post<Preview>("/api/v1/agents/preview", { variant, node });
export const estimateRun = (variant: string, suite: string) =>
  post<Estimate>("/api/v1/agents/estimate", { variant, suite });
export const startRun = (variant: string, suite: string, confirm_cost: number, over_cap = false) =>
  post<{ run: string; result: string }>("/api/v1/agents/runs", { variant, suite, confirm_cost, over_cap });
export const buildSuite = (body: { name: string; start: string; end: string; count: number; picks: number; controls: number }) =>
  post<{ result: string }>("/api/v1/agents/suites", body);

export const blankVariant = (): Variant => ({
  name: "",
  description: "",
  edits: [],
  models: {},
  settings: {},
  vendors: {},
  extra_tools: {},
  version: 0,
  saved: "",
});

/** The graph's stages, for grouping the agent list. */
export const STAGES: { label: string; nodes: string[] }[] = [
  { label: "Analysts", nodes: ["Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst"] },
  { label: "Research", nodes: ["Bull Researcher", "Bear Researcher", "Research Manager"] },
  { label: "Trading", nodes: ["Trader"] },
  { label: "Risk", nodes: ["Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"] },
  { label: "Decision", nodes: ["Portfolio Manager"] },
];
