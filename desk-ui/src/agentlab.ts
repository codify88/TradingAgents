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
  knobs?: Record<string, string>;
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

// --- knobs and replays (agentlab/knobs.py, agentlab/replay.py) -----------------------

export interface KnobOption {
  key: string;
  label: string;
  detail: string;
  agents: string[];
  settings: Record<string, number | boolean>;
}

export interface Knob {
  key: string;
  label: string;
  question: string;
  options: KnobOption[];
}

export interface ReplayCases {
  total: number;
  by_mandate: Record<string, Record<string, number>>;
  stages: Record<string, string>;
  max: number;
}

export interface ReplayRequest {
  variant: string;
  stage: string;
  mandate: string;
  count: number;
  seed: number;
  horizon: number;
  case_ids?: string[];
}

export interface ReplayEstimate {
  stage: string;
  cases: number;
  models: string[];
  cost: number | null;
  minutes: number;
  max_cost: number;
}

export interface Edge {
  scored: number;
  bullish_n: number;
  edge: number | null;
  t: number | null;
  hit_rate: number | null;
  calls: number;
}

export interface ReplayMetrics {
  id: string;
  variant: string;
  version: number;
  knobs: Record<string, string>;
  stage: string;
  horizon: number;
  status: string;
  started: string;
  finished: string | null;
  cases: number;
  done: number;
  failed: number;
  errors: string[];
  ratings: Record<string, number>;
  original_ratings: Record<string, number>;
  moves: Record<string, Record<string, number>>;
  bullish_share: number | null;
  hold_share: number | null;
  new: Edge;
  original: Edge;
  probability: { n: number; brier: number | null; mean: number | null } | null;
  cost: number | null;
  per_case: number | null;
  models: string[];
  case_ids: string[];
}

export interface ReplayDecision {
  case: string;
  ticker: string;
  date: string;
  mandate: string;
  original: string;
  rating: string | null;
  research: string | null;
  probability: number | null;
  status: string;
  error: string | null;
  decision: string | null;
  alpha: number | null;
}

export const getKnobs = () => get<{ knobs: Knob[]; production: Record<string, string> }>("/api/v1/agents/knobs");
export const saveKnobVariant = (body: { name: string; choices: Record<string, string>; description?: string; models?: { deep?: string; quick?: string } }) =>
  post<Variant>("/api/v1/agents/knobs", body);
export const getReplayCases = () => get<ReplayCases>("/api/v1/agents/replay/cases");
export const estimateReplay = (r: ReplayRequest) => post<ReplayEstimate>("/api/v1/agents/replay/estimate", r);
export const startReplay = (r: ReplayRequest & { confirm_cost: number; over_cap?: boolean }) =>
  post<{ replay: string; result: string }>("/api/v1/agents/replays", r);
export const getReplays = () => get<{ replays: ReplayMetrics[] }>("/api/v1/agents/replays");
export const getReplayDecisions = (id: string) =>
  get<{ decisions: ReplayDecision[] }>(`/api/v1/agents/replays/${encodeURIComponent(id)}/decisions`);
export const getPlaybook = () => get<{ text: string }>("/api/v1/agents/playbook");

export const blankVariant = (): Variant => ({
  name: "",
  description: "",
  edits: [],
  models: {},
  settings: {},
  vendors: {},
  extra_tools: {},
  knobs: {},
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
