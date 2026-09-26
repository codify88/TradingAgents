import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { passkeyMessage, reconcile, withPasskey, type MoneyAction } from "../api";
import { useToast } from "../components/toast";
import { Button, Card, Empty, ErrorNote, Label, Pill } from "../components/ui";
import { day, money, time } from "../format";
import { useRefresh, useSession, useTrading } from "../hooks";

export function BookScreen() {
  const { data, error, isPending } = useTrading();
  const session = useSession();
  const toast = useToast();
  const refresh = useRefresh();
  const [busy, setBusy] = useState<string | null>(null);
  const enrolled = (session.data?.passkeys ?? 0) > 0;

  async function run(name: string, fn: () => Promise<{ result: string }>) {
    setBusy(name);
    try {
      toast((await fn()).result);
      refresh();
    } catch (e) {
      toast(passkeyMessage(e), "error");
    } finally {
      setBusy(null);
    }
  }
  const money_ = (action: MoneyAction) => () => withPasskey(action);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-[26px] font-bold tracking-tight">Book</h1>
        <p className="text-[14px] text-muted">The standard strategy's paper account, by cohort.</p>
      </div>
      {error ? <ErrorNote error={error} /> : null}
      {isPending ? <div className="h-40 animate-pulse rounded-2xl bg-sunk" /> : null}
      {data ? (
        <>
          <Card>
            <div className="flex items-center justify-between gap-3">
              <Label>Trading</Label>
              {data.book.halted ? (
                <Pill tone="halt">HALTED</Pill>
              ) : data.book.blocked.length ? (
                <Pill tone="warn">BLOCKED</Pill>
              ) : (
                <Pill tone="gain">ALLOWED</Pill>
              )}
            </div>
            {data.refusal ? <p className="mt-2 text-[14px] text-muted">{data.refusal}</p> : null}
            {data.book.blocked.length ? (
              <ul className="mt-2 list-disc space-y-1 pl-5 text-[13px] text-muted">
                {data.book.blocked.map((b) => (
                  <li key={b}>{b}</li>
                ))}
              </ul>
            ) : null}
            <div className="mt-3 flex flex-wrap gap-2">
              {data.book.halted ? (
                <Button tone="primary" disabled={!enrolled || !!busy} onClick={() => run("resume", money_("resume"))}>
                  {busy === "resume" ? "Waiting for your passkey…" : "Resume trading"}
                </Button>
              ) : null}
              {data.book.blocked.length ? (
                <Button tone="primary" disabled={!enrolled || !!busy} onClick={() => run("ack", money_("ack"))}>
                  {busy === "ack" ? "Waiting for your passkey…" : "Acknowledge, I checked the broker"}
                </Button>
              ) : null}
              <Button disabled={!data.broker.available || !!busy} onClick={() => run("reconcile", reconcile)}>
                {busy === "reconcile" ? "Reconciling…" : "Reconcile now"}
              </Button>
            </div>
            {(data.book.halted || data.book.blocked.length) && !enrolled ? (
              <p className="mt-2 text-[13px] text-muted">
                Resuming and acknowledging need a passkey. <Link to="/security" className="font-semibold text-accent">Enrol one</Link>.
              </p>
            ) : null}
            <p className="mt-3 text-[12px] text-faint">
              Last reconciled {data.book.last_reconciled ? `${day(data.book.last_reconciled)} ${time(data.book.last_reconciled)}` : "never"}
            </p>
          </Card>

          <Card>
            <Label>Account</Label>
            {data.broker.account ? (
              <dl className="mt-2 grid grid-cols-3 gap-3">
                {(
                  [
                    ["Equity", data.broker.account.equity],
                    ["Cash", data.broker.account.cash],
                    ["Long", data.broker.account.long_market_value],
                  ] as const
                ).map(([k, v]) => (
                  <div key={k}>
                    <dt className="text-[12px] text-muted">{k}</dt>
                    <dd className="num mt-0.5 text-[16px] font-medium">{money(v)}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="mt-2 text-[14px] text-muted">
                Broker unavailable: {data.broker.error ?? "unknown"}. Add the Alpaca paper keys to <span className="num">.env</span>.
              </p>
            )}
            {data.broker.account ? (
              <p className="mt-2 text-[12px] text-faint">{data.broker.account.paper ? "Paper account" : "LIVE account"}</p>
            ) : null}
          </Card>

          {data.pending ? (
            <Link to="/plan/$id" params={{ id: data.pending.id }}>
              <Card className="hover:bg-sunk">
                <Label>Pending plan</Label>
                <p className="mt-1 text-[15px] font-semibold">
                  {data.pending.orders.length} orders for the {day(data.pending.session)} open
                </p>
              </Card>
            </Link>
          ) : null}

          <section className="space-y-2">
            <h2 className="px-1 text-[15px] font-semibold">Live cohorts</h2>
            {data.book.live.length === 0 ? (
              <Empty title="No positions">Cohorts appear here once an approved plan fills.</Empty>
            ) : (
              data.book.live.map((c) => (
                <Card key={c.plan_id}>
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="font-semibold">Bought {day(c.entry_session)}</span>
                    <span className="text-[13px] text-muted">
                      {c.exit_plan ? "selling" : `exits ${day(c.exit_session)}`}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {Object.entries(c.shares).map(([s, q]) => (
                      <Pill key={s}>
                        {s} {q}
                      </Pill>
                    ))}
                  </div>
                </Card>
              ))
            )}
          </section>

          {data.events.length ? (
            <section className="space-y-2">
              <h2 className="px-1 text-[15px] font-semibold">Recent events</h2>
              <Card className="py-2">
                <ul className="divide-y divide-rule text-[13px]">
                  {data.events.slice(0, 12).map((e, i) => (
                    <li key={`${e.at}-${i}`} className="flex justify-between gap-3 py-2">
                      <span className="font-medium">{e.event}</span>
                      <span className="num text-faint">
                        {day(e.at)} {time(e.at)}
                      </span>
                    </li>
                  ))}
                </ul>
              </Card>
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
