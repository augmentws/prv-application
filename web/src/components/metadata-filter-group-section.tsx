"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { type ReactNode, useId, useState } from "react";

import { Badge } from "@/components/ui/badge";

export function MetadataFilterGroupSection({
  displayName,
  activeCount = 0,
  children,
}: {
  displayName: string;
  activeCount?: number;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(true);
  const contentId = useId();

  return (
    <section aria-label={`${displayName} filters`}>
      <h3>
        <button
          type="button"
          className="flex w-full items-center gap-2 border-b bg-muted/30 px-3 py-2 text-left text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground outline-none hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
          aria-label={`${displayName} filter group`}
          aria-expanded={open}
          aria-controls={contentId}
          onClick={() => setOpen((current) => !current)}
        >
          {open ? <ChevronDown className="size-3.5 shrink-0" /> : <ChevronRight className="size-3.5 shrink-0" />}
          <span className="min-w-0 flex-1 truncate" title={displayName}>{displayName}</span>
          {activeCount ? <Badge variant="accent" className="shrink-0">{activeCount}</Badge> : null}
        </button>
      </h3>
      {open ? <div id={contentId} className="divide-y">{children}</div> : null}
    </section>
  );
}
