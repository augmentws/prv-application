import { CircleHelp } from "lucide-react";
import Link from "next/link";

import { helpTopics, type HelpTopic } from "@/lib/help-topics";

export function HelpLink({ topic }: { topic: HelpTopic }) {
  const help = helpTopics[topic];

  return (
    <span className="group relative inline-flex">
      <Link
        href={help.href}
        aria-label={help.title}
        className="inline-flex size-9 items-center justify-center rounded-md border bg-background text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <CircleHelp className="size-4" />
      </Link>
      <span
        role="tooltip"
        className="pointer-events-none absolute right-0 top-full z-50 mt-2 hidden w-64 rounded-md border bg-popover px-3 py-2 text-left text-xs leading-5 text-popover-foreground shadow-lg group-hover:block group-focus-within:block"
      >
        {help.summary}
      </span>
    </span>
  );
}
