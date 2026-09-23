import type { ReactNode } from "react";

export function ResourcePageHeader({ breadcrumbs, title }: { breadcrumbs: ReactNode; title: string }) {
  return (
    <header aria-label={`${title} header`} className="mb-4">
      <div className="flex min-w-0 items-center justify-between gap-4">
        <nav aria-label="Breadcrumb" className="flex min-w-0 flex-wrap items-center gap-1 text-sm text-muted-foreground">
          {breadcrumbs}
        </nav>
        <h1 className="ml-auto shrink-0 text-right text-2xl font-semibold tracking-[-0.025em] text-foreground md:text-3xl">{title}</h1>
      </div>
    </header>
  );
}
