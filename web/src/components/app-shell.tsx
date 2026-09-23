"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQueryClient } from "@tanstack/react-query";
import {
  Building2,
  Bot,
  BookOpenText,
  ChevronsUpDown,
  FolderKanban,
  LayoutDashboard,
  LogOut,
  Menu,
  PanelLeftClose,
  Wrench,
  Users,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { BrandMark } from "@/components/brand-mark";
import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { WorkspaceProvider, useWorkspace } from "@/components/workspace-context";
import { logout } from "@/lib/api-client";
import { cn } from "@/lib/utils";

const primaryLinks = [
  { href: "/app", label: "Overview", icon: LayoutDashboard, exact: true },
  { href: "/app/clients", label: "Clients", icon: FolderKanban },
  { href: "/app/users", label: "Users", icon: Users },
  { href: "/docs", label: "Documentation", icon: BookOpenText },
];

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <WorkspaceProvider>
      <Shell>{children}</Shell>
    </WorkspaceProvider>
  );
}

function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user, tenant, tenants, selectedTenantId, setSelectedTenantId } = useWorkspace();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  async function signOut() {
    await logout();
    queryClient.clear();
    router.replace("/login");
    router.refresh();
  }

  const navigation = user.is_superuser
    ? [
        ...primaryLinks,
        { href: "/admin/tenants", label: "Tenants", icon: Building2 },
        { href: "/admin/agents", label: "Agents", icon: Bot },
        { href: "/admin/skills", label: "Skills", icon: Wrench },
      ]
    : primaryLinks;

  const sidebar = (
    <aside className={cn("flex h-full flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground", collapsed ? "w-[4.75rem]" : "w-64")}>
      <div className="flex h-16 items-center gap-3 border-b border-sidebar-border px-4">
        <BrandMark className="size-9 shrink-0" />
        {!collapsed ? <span className="text-base font-bold tracking-tight">Priv-View</span> : null}
        <Button
          className={cn("ml-auto hidden lg:inline-flex", collapsed && "absolute left-[4.1rem] top-3.5 z-20 bg-card shadow-md")}
          variant="ghost"
          size="icon"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={() => setCollapsed((value) => !value)}
        >
          <PanelLeftClose className={cn(collapsed && "rotate-180")} />
        </Button>
        <Button className="ml-auto lg:hidden" variant="ghost" size="icon" aria-label="Close navigation" onClick={() => setMobileOpen(false)}>
          <X />
        </Button>
      </div>

      <div className="border-b border-sidebar-border p-3">
        {collapsed ? (
          <div className="grid size-10 place-items-center rounded-lg bg-primary/10 font-bold text-primary" title={tenant.name}>
            {tenant.name.slice(0, 1).toUpperCase()}
          </div>
        ) : user.is_superuser && tenants.length > 1 ? (
          <div>
            <p className="mb-1.5 px-1 text-xs font-semibold uppercase tracking-[0.09em] text-muted-foreground">Tenant</p>
            <Select value={selectedTenantId} onValueChange={setSelectedTenantId}>
              <SelectTrigger className="border-sidebar-border bg-card"><SelectValue /></SelectTrigger>
              <SelectContent>
                {tenants.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        ) : (
          <div className="rounded-lg border border-sidebar-border bg-card px-3 py-2">
            <p className="truncate text-sm font-semibold">{tenant.name}</p>
            <p className="truncate text-xs text-muted-foreground">{tenant.slug}</p>
          </div>
        )}
      </div>

      <nav aria-label="Primary" className="flex-1 space-y-1 overflow-y-auto p-3">
        {navigation.map(({ href, label, icon: Icon, exact }) => {
          const active = exact ? pathname === href : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              title={collapsed ? label : undefined}
              aria-current={active ? "page" : undefined}
              onClick={() => setMobileOpen(false)}
              className={cn(
                "flex h-10 items-center gap-3 rounded-lg px-3 text-sm font-medium outline-none transition hover:bg-card focus-visible:ring-2 focus-visible:ring-ring",
                active && "bg-primary text-primary-foreground shadow-sm hover:bg-primary",
                collapsed && "justify-center px-0",
              )}
            >
              <Icon className="size-[1.1rem] shrink-0" />
              {!collapsed ? <span>{label}</span> : null}
            </Link>
          );
        })}
      </nav>

      <div className="border-t border-sidebar-border p-3">
        <DropdownMenu.Root>
          <DropdownMenu.Trigger asChild>
            <button className={cn("flex w-full items-center gap-3 rounded-lg p-2 text-left outline-none hover:bg-card focus-visible:ring-2 focus-visible:ring-ring", collapsed && "justify-center")}>
              <span className="grid size-9 shrink-0 place-items-center rounded-full bg-primary text-xs font-bold text-primary-foreground">
                {user.display_name.split(" ").map((part) => part[0]).join("").slice(0, 2).toUpperCase()}
              </span>
              {!collapsed ? (
                <>
                  <span className="min-w-0 flex-1"><span className="block truncate text-sm font-semibold">{user.display_name}</span><span className="block truncate text-xs text-muted-foreground">{user.email}</span></span>
                  <ChevronsUpDown className="size-4 text-muted-foreground" />
                </>
              ) : null}
            </button>
          </DropdownMenu.Trigger>
          <DropdownMenu.Portal>
            <DropdownMenu.Content side="top" align="start" className="z-50 mb-2 min-w-52 rounded-lg border bg-popover p-1 text-popover-foreground shadow-xl">
              <DropdownMenu.Item onSelect={() => void signOut()} className="flex cursor-default items-center gap-2 rounded-md px-3 py-2 text-sm text-destructive outline-none focus:bg-muted">
                <LogOut className="size-4" /> Sign out
              </DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      </div>
    </aside>
  );

  return (
    <div className="flex h-dvh min-h-0 overflow-hidden bg-background">
      <div className="hidden h-full shrink-0 lg:block">{sidebar}</div>
      {mobileOpen ? <div className="fixed inset-0 z-40 bg-foreground/35 backdrop-blur-sm lg:hidden" onClick={() => setMobileOpen(false)} /> : null}
      <div className={cn("fixed inset-y-0 left-0 z-50 w-64 transition-transform lg:hidden", mobileOpen ? "translate-x-0" : "-translate-x-full")}>{sidebar}</div>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <header className="z-30 flex h-16 shrink-0 items-center gap-3 border-b bg-background/92 px-4 backdrop-blur md:px-6">
          <Button variant="ghost" size="icon" aria-label="Open navigation" className="lg:hidden" onClick={() => setMobileOpen(true)}><Menu /></Button>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold">{tenant.name}</p>
            <p className="truncate text-xs text-muted-foreground">Secure workspace</p>
          </div>
          <ThemeToggle />
        </header>
        <main
          id="main-content"
          className={cn(
            "mx-auto min-h-0 w-full flex-1 overflow-x-hidden overflow-y-auto p-4 md:p-6 lg:p-8",
            collapsed ? "max-w-none" : "max-w-[96rem]",
          )}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
