import { get, post } from "./api";

// --- shapes from tradingagents/desk/research_api.py --------------------------------

export interface SymbolHit {
  symbol: string;
  name: string;
  type: string;
  exchange: string;
}

export interface ResearchMeta {
  groups: { key: string; title: string }[];
  mandates: string[];
  models: string[];
  default_models: { deep: string; quick: string };
  indices: { symbol: string; name: string }[];
}

export type SectionStatus = "pending" | "ok" | "empty" | "unavailable" | "error";

export interface Section {
  key: string;
  group: string;
  title: string;
  tool: string;
  args: Record<string, unknown>;
  vendor: string | null;
  status: SectionStatus;
  text: string;
  seconds: number;
}

export interface FetchData {
  symbol: string;
  date: string;
  status: "running" | "finished";
  started: string;
  finished?: string;
  sections: Section[];
}

export interface ResearchRun {
  id: string;
  symbol: string;
  date: string;
  mandate: string;
  models: { deep?: string; quick?: string };
  status: "running" | "finished" | "failed";
  started: string;
  finished?: string;
  rating?: string;
  error?: string;
}

export interface Overview {
  symbol: string;
  name: SymbolHit | null;
  fetches: { date: string; status: string; counts: Record<SectionStatus, number> }[];
  runs: ResearchRun[];
  fetching: string[];
}

export interface BookDecision {
  date: string;
  rating: string;
  mandate: string | null;
  source: string;
  pending: boolean | null;
  alpha: number | string | null;
  holding: string | null;
}

export interface RunEstimate {
  models: string[];
  cost_low: number | null;
  cost_high: number | null;
  minutes: number;
}

// --- calls -------------------------------------------------------------------------

const enc = encodeURIComponent;

export const searchSymbols = (q: string) => get<{ results: SymbolHit[] }>(`/api/v1/research/search?q=${enc(q)}`);
export const getResearchMeta = () => get<ResearchMeta>("/api/v1/research/meta");
export const getOverview = (s: string) => get<Overview>(`/api/v1/research/${enc(s)}`);
export const getFetch = (s: string, day: string) => get<FetchData>(`/api/v1/research/${enc(s)}/data/${day}`);
export const startFetch = (s: string, date: string) =>
  post<{ result: string; date: string }>(`/api/v1/research/${enc(s)}/fetch`, { date });
export const estimateResearch = (models: { deep?: string; quick?: string }) =>
  post<RunEstimate>("/api/v1/research/estimate", { models });
export const startResearchRun = (
  s: string,
  body: { date: string; mandate: string; models: { deep?: string; quick?: string }; confirm_cost: number },
) => post<{ run: string; result: string }>(`/api/v1/research/${enc(s)}/runs`, body);
export const getResearchReport = (s: string, id: string) =>
  get<ResearchRun & { report: string }>(`/api/v1/research/${enc(s)}/runs/${enc(id)}`);
export const getBookDecisions = (s: string) => get<{ decisions: BookDecision[] }>(`/api/v1/research/${enc(s)}/decisions`);
export const getBookReport = (s: string, day: string) =>
  get<{ text: string }>(`/api/v1/research/${enc(s)}/decisions/${day}`);

export const MANDATE_LABEL: Record<string, string> = {
  "": "No mandate (standard)",
  equity_value: "Value",
  equity_momentum: "Momentum",
  equity_momentum_leaps: "Momentum LEAPS",
};
