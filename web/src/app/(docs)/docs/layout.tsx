import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { DocsLayout } from "fumadocs-ui/layouts/docs";
import { RootProvider } from "fumadocs-ui/provider/next";

import { ThemeToggle } from "@/components/theme-toggle";
import { docsSource } from "@/lib/docs-source";

export default function DocumentationLayout({ children }: { children: ReactNode }) {
  return (
    <RootProvider
      theme={{ enabled: false }}
      search={{ options: { api: "/api/search" } }}
    >
      <DocsLayout
        tree={docsSource.pageTree}
        nav={{ title: "Priv-View Help", url: "/docs" }}
        themeSwitch={{ enabled: false }}
        sidebar={{
          footer: (
            <div className="space-y-2 border-t border-fd-border pt-3">
              <Link href="/app" className="flex h-10 items-center gap-2 rounded-lg border border-fd-border bg-fd-secondary/50 px-3 text-sm font-semibold text-fd-foreground transition-colors hover:bg-fd-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-fd-ring">
                <ArrowLeft className="size-4" />
                Back to app
              </Link>
              <div className="flex min-h-10 items-center justify-between gap-3 rounded-lg px-3 text-sm text-fd-muted-foreground">
                <span>Appearance</span>
                <ThemeToggle />
              </div>
            </div>
          ),
        }}
      >
        {children}
      </DocsLayout>
    </RootProvider>
  );
}
