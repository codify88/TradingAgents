import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { Card, Empty, ErrorNote, Label, Pill } from "../components/ui";
import { day, money, time } from "../format";
import { useToday, useTrading } from "../hooks";

export function PlanDetailScreen() {
  const { id } = useParams({ from: "/shell/plan/$id" });
  const today = useToday();
  const trading = useTrading();
  const plan = today.data?.plan?.id === id ? today.data.plan : trading.data?.pending?.id === id ? trading.data.pending : null;
  const summary = trading.data?.plans.find((p) => p.id === id);
  const account = trading.data?.broker.account;

  return (
    <div className="space-y-4">
      <Link to="/" className="inline-flex items-center gap-1 text-[14px] font-medium text-accent">
        <ArrowLeft size={16} /> Today
      </Link>
      {today.error ? <ErrorNote error={today.error} /> : null}
      {!plan ? (
        <Empty title={summary ? `Plan ${summary.status}` : "No such pending plan"}>
          {summary
            ? `${summary.orders} orders for the ${summary.session} open${summary.submitted ? `, submitted ${time(summary.submitted)}` : ""}.`
            : "It may have expired or already been submitted."}
        </Empty>
      ) : (
        <>
          <div>
            <h1 className="text-[24px] font-bold tracking-tight">Order plan</h1>
            <p className="num text-[12px] text-muted">{plan.id}</p>
          </div>
          <Card>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-[14px]">
              <div>
                <Label>Open</Label>
                <dd className="mt-1">{day(plan.session)}</dd>
              </div>
              <div>
                <Label>Cutoff</Label>
                <dd className="num mt-1">{time(plan.expires)} NY</dd>
              </div>
              <div>
                <Label>Decisions of</Label>
                <dd className="mt-1">{day(plan.decision_date)}</dd>
              </div>
              <div>
                <Label>New cohort exits</Label>
                <dd className="mt-1">{plan.exit_session ? day(plan.exit_session) : "—"}</dd>
              </div>
              <div>
                <Label>Equity at planning</Label>
                <dd className="num mt-1">{money(plan.equity)}</dd>
              </div>
              <div>
                <Label>Cash now</Label>
                <dd className="num mt-1">{account ? money(account.cash) : "broker unavailable"}</dd>
              </div>
            </dl>
          </Card>
          <Card>
            <Label>Orders · market-on-open</Label>
            <ul className="mt-2 divide-y divide-rule">
              {plan.orders.map((o) => (
                <li key={`${o.side}-${o.symbol}`} className="flex items-start justify-between gap-3 py-3">
                  <div className="flex items-start gap-3">
                    <Pill tone={o.side === "buy" ? "gain" : "loss"}>{o.side.toUpperCase()}</Pill>
                    <div>
                      <div className="font-semibold">{o.symbol}</div>
                      <div className="text-[13px] text-muted">{o.reason}</div>
                    </div>
                  </div>
                  <span className="num text-[14px]">{o.qty.toLocaleString()} sh</span>
                </li>
              ))}
            </ul>
            {Object.keys(plan.carried).length ? (
              <p className="mt-2 text-[13px] text-muted">
                Carried into the new cohort, not sold and bought back:{" "}
                {Object.entries(plan.carried)
                  .map(([s, q]) => `${s} ${q}`)
                  .join(", ")}
              </p>
            ) : null}
          </Card>
          {plan.notes.length ? (
            <Card>
              <Label>Notes from planning</Label>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-[14px] text-muted">
                {plan.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            </Card>
          ) : null}
          <p className="text-[13px] text-faint">Approve from Today: hold the button, then confirm with your passkey.</p>
        </>
      )}
    </div>
  );
}
