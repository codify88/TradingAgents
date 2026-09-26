import { useQuery, useQueryClient } from "@tanstack/react-query";
import { get, getSession, type Passkey, type Today, type Trading } from "./api";

// Today refreshes every 30 seconds and whenever the app comes back to the
// foreground: the plan's cutoff and a halt must never be read stale.
export const useToday = () =>
  useQuery({ queryKey: ["today"], queryFn: () => get<Today>("/api/v1/today"), refetchInterval: 30_000 });

export const useTrading = () =>
  useQuery({ queryKey: ["trading"], queryFn: () => get<Trading>("/api/v1/trading"), refetchInterval: 60_000 });

export const useSession = () => useQuery({ queryKey: ["session"], queryFn: () => getSession(true) });

export const usePasskeys = () =>
  useQuery({
    queryKey: ["passkeys"],
    queryFn: () => get<{ host: string; passkeys: Passkey[] }>("/api/v1/passkeys"),
  });

/** After any action: everything a screen shows may have changed. */
export function useRefresh() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries();
}
