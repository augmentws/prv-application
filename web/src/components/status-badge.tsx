import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const active = new Set(["ACTIVE", "OPEN", "READY", "FINALIZED", "COMPLETED"]);
  const pending = new Set(["QUEUED", "SNAPSHOTTING", "PLANNING", "RUNNING", "WAITING", "DELETING", "DELETING_DATABASE_ROWS", "DELETING_BLOBS", "VALIDATING", "WAITING_APPROVAL", "AWAITING_REVIEW", "APPROVED"]);
  const variant = active.has(status) ? "active" : pending.has(status) ? "accent" : "outline";
  const problem = status === "FAILED" || status === "COMPLETED_WITH_ERRORS";
  const label = status.toLowerCase().replaceAll("_", " ");
  return <Badge variant={variant} className={cn("max-w-full", problem && "border-destructive/30 text-destructive", className)}><span className="min-w-0 truncate" title={label}>{label}</span></Badge>;
}
