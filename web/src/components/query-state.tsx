import { AlertTriangle, Inbox } from "lucide-react";

import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export function TableLoading() {
  return <Card className="space-y-3 p-5">{Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="h-10 w-full" />)}</Card>;
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return (
    <Card className="grid min-h-52 place-items-center p-8 text-center">
      <div><span className="mx-auto grid size-11 place-items-center rounded-xl bg-muted text-muted-foreground"><Inbox /></span><h2 className="mt-4 font-semibold">{title}</h2><p className="mt-1.5 max-w-sm text-sm leading-6 text-muted-foreground">{description}</p></div>
    </Card>
  );
}

export function QueryError({ message = "This information could not be loaded." }: { message?: string }) {
  return (
    <Card className="flex items-center gap-3 border-destructive/30 p-5 text-destructive"><AlertTriangle className="size-5 shrink-0" /><p className="text-sm">{message}</p></Card>
  );
}
