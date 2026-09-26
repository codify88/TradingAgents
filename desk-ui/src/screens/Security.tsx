import { useState } from "react";
import { browserSupportsWebAuthn } from "@simplewebauthn/browser";
import { enrolPasskey, passkeyMessage } from "../api";
import { useToast } from "../components/toast";
import { Button, Card, ErrorNote, Label } from "../components/ui";
import { day } from "../format";
import { usePasskeys, useRefresh, useSession } from "../hooks";

function guessDevice(): string {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android phone";
  if (/Macintosh/.test(ua)) return "Mac";
  return "This device";
}

export function SecurityScreen() {
  const session = useSession();
  const keys = usePasskeys();
  const toast = useToast();
  const refresh = useRefresh();
  const [code, setCode] = useState("");
  const [label, setLabel] = useState(guessDevice);
  const [busy, setBusy] = useState(false);
  const supported = browserSupportsWebAuthn();

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-[26px] font-bold tracking-tight">Security</h1>
        <p className="text-[14px] text-muted">
          Approving a plan, resuming and acknowledging each ask for your passkey (Face ID or Touch ID). Halting never does.
        </p>
      </div>

      <Card>
        <Label>Passkeys for {session.data?.host ?? "this address"}</Label>
        {keys.error ? <ErrorNote error={keys.error} /> : null}
        {keys.data && keys.data.passkeys.length === 0 ? (
          <p className="mt-2 text-[14px] text-muted">None yet. Enrol one below to approve plans from this address.</p>
        ) : (
          <ul className="mt-2 divide-y divide-rule">
            {keys.data?.passkeys.map((k) => (
              <li key={k.id} className="flex items-baseline justify-between gap-3 py-2.5 text-[14px]">
                <span className="font-semibold">{k.label}</span>
                <span className="text-[13px] text-muted">
                  added {day(k.created)}
                  {k.last_used ? ` · used ${day(k.last_used)}` : ""}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-[12px] text-faint">
          A passkey works only at the address it was made on: enrol once at localhost on the Mac, and once at the Mac's
          Tailscale address from your phone.
        </p>
      </Card>

      <Card>
        <Label>Enrol this device</Label>
        {!supported ? (
          <p className="mt-2 text-[14px] text-muted">This browser can't use passkeys.</p>
        ) : (
          <form
            className="mt-3 space-y-3"
            onSubmit={async (e) => {
              e.preventDefault();
              setBusy(true);
              try {
                const k = await enrolPasskey(code, label.trim() || guessDevice());
                toast(`Passkey enrolled: ${k.label}.`);
                setCode("");
                refresh();
              } catch (err) {
                toast(passkeyMessage(err), "error");
              } finally {
                setBusy(false);
              }
            }}
          >
            <div>
              <label htmlFor="enrol-code" className="text-[14px] text-muted">
                Enrolment code: run <span className="num text-ink">tradingagents desk code</span> on the Mac
              </label>
              <input
                id="enrol-code"
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                autoComplete="one-time-code"
                autoCapitalize="characters"
                placeholder="ABCD-EFGH"
                className="num mt-1 w-full rounded-xl border border-rule bg-ground px-3 py-3 text-[17px] tracking-[0.15em] outline-none focus:border-accent"
              />
            </div>
            <div>
              <label htmlFor="enrol-label" className="text-[14px] text-muted">
                Name for this device
              </label>
              <input
                id="enrol-label"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                className="mt-1 w-full rounded-xl border border-rule bg-ground px-3 py-3 text-[15px] outline-none focus:border-accent"
              />
            </div>
            <Button tone="primary" type="submit" disabled={busy || code.replace(/[^A-Z0-9]/g, "").length < 8} className="w-full">
              {busy ? "Waiting for your passkey…" : "Create passkey"}
            </Button>
          </form>
        )}
      </Card>
    </div>
  );
}
