"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { type ReactNode, useCallback, useId, useSyncExternalStore } from "react";

import { Badge } from "@/components/ui/badge";

const FILTER_GROUP_STATE_EVENT = "priv-view:metadata-filter-group-state";

function storedOpen(storageKey: string) {
  try {
    return window.localStorage.getItem(storageKey) === "open";
  } catch {
    return false;
  }
}

export function MetadataFilterGroupSection({
  displayName,
  storageKey,
  activeCount = 0,
  children,
}: {
  displayName: string;
  storageKey: string;
  activeCount?: number;
  children: ReactNode;
}) {
  const contentId = useId();
  const subscribe = useCallback((notify: () => void) => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === storageKey) notify();
    };
    const onLocalChange = (event: Event) => {
      if ((event as CustomEvent<string>).detail === storageKey) notify();
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener(FILTER_GROUP_STATE_EVENT, onLocalChange);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener(FILTER_GROUP_STATE_EVENT, onLocalChange);
    };
  }, [storageKey]);
  const getSnapshot = useCallback(() => storedOpen(storageKey), [storageKey]);
  const open = useSyncExternalStore(subscribe, getSnapshot, () => false);

  const toggle = () => {
    const next = !open;
    try {
      window.localStorage.setItem(storageKey, next ? "open" : "closed");
    } catch {
      return;
    }
    window.dispatchEvent(new CustomEvent(FILTER_GROUP_STATE_EVENT, { detail: storageKey }));
  };

  return (
    <section aria-label={`${displayName} filters`}>
      <h3>
        <button
          type="button"
          className="flex w-full items-center gap-2 border-b bg-muted/30 px-3 py-2 text-left text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground outline-none hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
          aria-label={`${displayName} filter group`}
          aria-expanded={open}
          aria-controls={contentId}
          onClick={toggle}
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
