import { Badge } from "@/components/ui/badge";

export function StatusBadge({ status }: { status: string }) {
  const active = new Set(["ACTIVE", "OPEN", "READY", "FINALIZED", "COMPLETED"]);
  const pending = new Set(["QUEUED", "SNAPSHOTTING", "PLANNING", "RUNNING", "WAITING_APPROVAL", "APPROVED"]);
  const variant = active.has(status) ? "active" : pending.has(status) ? "accent" : "outline";
  const problem = status === "FAILED" || status === "COMPLETED_WITH_ERRORS";
  return <Badge variant={variant} className={problem ? "border-destructive/30 text-destructive" : undefined}>{status.toLowerCase().replaceAll("_", " ")}</Badge>;
}
