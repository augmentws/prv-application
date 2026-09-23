import type { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex min-w-0 flex-col justify-between gap-4 border-b pb-6 sm:flex-row sm:items-end">
      <div className="min-w-0">
        {eyebrow ? <p className="mb-1.5 text-xs font-bold uppercase tracking-[0.12em] text-primary">{eyebrow}</p> : null}
        <h1 className="text-2xl font-semibold tracking-[-0.025em] text-foreground md:text-3xl">{title}</h1>
        {description ? <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground md:text-base">{description}</p> : null}
      </div>
      {actions ? <div className="flex min-w-0 flex-wrap items-center gap-2 sm:justify-end">{actions}</div> : null}
    </div>
  );
}
