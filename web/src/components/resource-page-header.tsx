import type { ReactNode } from "react";

/**
 * Breadcrumb on the left; the resource title on the right. `leading` sits immediately left of the title
 * (help, status); `action` replaces the title entirely.
 */
export function ResourcePageHeader({ breadcrumbs, title, action, leading }: { breadcrumbs: ReactNode; title: string; action?: ReactNode; leading?: ReactNode }) {
  return (
    <header aria-label={`${title} header`} className="mb-4">
      <div className="flex min-w-0 items-center justify-between gap-4">
        <nav aria-label="Breadcrumb" className="flex min-w-0 flex-wrap items-center gap-1 text-sm text-muted-foreground">
          {breadcrumbs}
        </nav>
        {action ? <div className="ml-auto shrink-0">{action}</div>
          : leading ? (
            <div className="ml-auto flex min-w-0 shrink-0 items-center gap-3">
              <div className="flex shrink-0 items-center gap-2">{leading}</div>
              <h1 className="text-right text-2xl font-semibold tracking-[-0.025em] text-foreground md:text-3xl">{title}</h1>
            </div>
          )
          : <h1 className="ml-auto shrink-0 text-right text-2xl font-semibold tracking-[-0.025em] text-foreground md:text-3xl">{title}</h1>}
      </div>
    </header>
  );
}
