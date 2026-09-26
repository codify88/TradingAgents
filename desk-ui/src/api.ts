import { startAuthentication, startRegistration } from "@simplewebauthn/browser";

// --- shapes the Desk API returns (tradingagents/desk/today.py, views.py) ---------

export type Severity = "critical" | "action" | "attention" | "info";

export interface InboxItem {
  kind: "halted" | "blocked" | "night" | "plan" | "reviews" | "suggestion";
  severity: Severity;
  title: string;
  detail: string;
  plan_id?: string;
  strategy?: string;
}

export interface PlannedOrder {
  symbol: string;
  side: "buy" | "sell";
  qty: number;
  reason: string;
}

export interface OrderPlan {
  id: string;
  strategy: string;
  decision_date: string;
  session: string;
  expires: string;
  created: string;
  equity: number;
  orders: PlannedOrder[];
  exit_cohorts: string[];
  notes: string[];
  status: string;
  exit_session: string;
  carried: Record<string, number>;
  seconds_left?: number;
  buys?: number;
  sells?: number;
}

export interface Switches {
  halted: boolean;
  halted_reason: string;
  blocked: string[];
  refusal: string | null;
}

export interface Today {
  plan: OrderPlan | null;
  switches: Switches;
  night: null | {
    started: string;
    finished: string | null;
    exit_code: number | null;
    names_run: number;
    failures: number;
    problems: string[];
    summary: string;
  };
  reviews: { when: string; ticker: string; decided: string; days: number; mandate: string; overdue: boolean }[];
  suggestions: { strategy_key: string; candidate: string; reason: string; tradeable: boolean }[];
  inbox: InboxItem[];
  needs_you: number;
}

export interface Cohort {
  plan_id: string;
  strategy: string;
  entry_session: string;
  exit_session: string;
  exit_plan: string | null;
  shares: Record<string, number>;
}

export interface Trading {
  book: { halted: boolean; halted_reason: string; blocked: string[]; last_reconciled: string; live: Cohort[] };
  refusal: string | null;
  pending: OrderPlan | null;
  plans: { id: string; status: string; session: string; decision_date: string; orders: number; submitted: string }[];
  plan_error: string | null;
  events: { at: string; event: string; [k: string]: unknown }[];
  broker: {
    available: boolean;
    error: string | null;
    account: null | { equity: number; cash: number; buying_power: number; long_market_value: number; paper: boolean };
  };
}

export interface Session {
  token: string;
  host: string;
  passkeys: number;
  on_the_mac: boolean;
  money_actions: string[];
}

export interface Passkey {
  id: string;
  label: string;
  created: string;
  last_used?: string | null;
}

// --- transport -------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

let session: Promise<Session> | null = null;

/** The session: the action token and whether a passkey is enrolled at this address.
 *  Re-read after a 403, since the token changes whenever Desk restarts. */
export function getSession(refresh = false): Promise<Session> {
  if (!session || refresh) {
    session = get<Session>("/api/v1/session").catch((e) => {
      session = null;
      throw e;
    });
  }
  return session;
}

async function unwrap<T>(r: Response): Promise<T> {
  const text = await r.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    /* not JSON: say what the status was */
  }
  if (!r.ok) {
    const msg = (body as { error?: string } | null)?.error ?? `Desk answered ${r.status}`;
    throw new ApiError(msg, r.status);
  }
  return body as T;
}

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
  return unwrap<T>(r);
}

export async function post<T>(path: string, body: unknown = {}, retried = false): Promise<T> {
  const { token } = await getSession();
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-Desk-Token": token },
    body: JSON.stringify(body),
  });
  if (r.status === 403 && !retried) {
    await getSession(true); // Desk restarted: a new token
    return post<T>(path, body, true);
  }
  return unwrap<T>(r);
}

// --- actions ---------------------------------------------------------------------

export type MoneyAction = "submit" | "resume" | "ack";

/** A money action: a passkey prompt made for exactly this action (and plan), then the action. */
export async function withPasskey(action: MoneyAction, body: { plan_id?: string } = {}) {
  const options = await post<Parameters<typeof startAuthentication>[0]["optionsJSON"]>("/api/v1/passkeys/options", {
    action,
    subject: action === "submit" ? body.plan_id ?? "" : "",
  });
  const assertion = await startAuthentication({ optionsJSON: options });
  return post<{ result: string }>(`/api/v1/trade/${action}`, { ...body, assertion });
}

export function halt(reason: string) {
  return post<{ result: string }>("/api/v1/trade/halt", { reason });
}

export function reconcile() {
  return post<{ result: string }>("/api/v1/trade/reconcile", {});
}

export async function enrolPasskey(code: string, label: string) {
  const options = await post<Parameters<typeof startRegistration>[0]["optionsJSON"]>(
    "/api/v1/passkeys/register/options",
    { code },
  );
  const credential = await startRegistration({ optionsJSON: options });
  const saved = await post<Passkey>("/api/v1/passkeys/register/verify", { credential, label });
  await getSession(true);
  return saved;
}

/** A browser that refused or cancelled the passkey prompt, in words. */
export function passkeyMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error && (e.name === "NotAllowedError" || e.name === "AbortError"))
    return "The passkey prompt was cancelled or timed out. Nothing was sent.";
  if (e instanceof Error) return e.message;
  return "Something went wrong. Nothing was sent.";
}
