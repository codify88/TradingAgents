import { useQuery } from "@tanstack/react-query";
import { getPlaybook } from "../../agentlab";
import { Markdownish } from "../../components/Md";
import { Card, ErrorNote } from "../../components/ui";

/** docs/design/rating-playbook.md, as served by Desk. */
export function PlaybookTab() {
  const q = useQuery({ queryKey: ["agentlab", "playbook"], queryFn: getPlaybook, staleTime: 60_000 });
  if (q.error) return <ErrorNote error={q.error} />;
  return (
    <Card className="mx-auto max-w-3xl px-6 py-5">
      {q.data ? <Markdownish text={q.data.text} className="md-wrap text-[14px]" /> : <p className="text-faint">Loading…</p>}
    </Card>
  );
}
