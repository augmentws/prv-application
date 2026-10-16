import { Badge } from "@/components/ui/badge";
import { FACT_STATUS_CLASSES, FACT_STATUS_LABELS } from "@/lib/facts";
import { cn } from "@/lib/utils";

export function FactStatusBadge({ status, className }: { status: string; className?: string }) {
  return (
    <Badge variant={null} className={cn(FACT_STATUS_CLASSES[status] ?? "border border-border text-muted-foreground", className)}>
      {FACT_STATUS_LABELS[status] ?? status.toLowerCase()}
    </Badge>
  );
}
