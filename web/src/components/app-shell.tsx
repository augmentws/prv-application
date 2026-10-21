"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQueryClient } from "@tanstack/react-query";
import {
  Building2,
  Bell,
  Bot,
  BookOpenText,
  ChevronDown,
  FolderKanban,
  LayoutGrid,
  LayoutDashboard,
  LogOut,
  Menu,
  PanelLeftClose,
  ScrollText,
  ShieldCheck,
  Wrench,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { BrandMark } from "@/components/brand-mark";
import { CollectionSidebarNav } from "@/components/collection/collection-sidebar-nav";
import { MatterSidebarNav } from "@/components/matter/matter-sidebar-nav";
import { NotificationBell } from "@/components/notification-bell";
import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { WorkspaceProvider, useWorkspace } from "@/components/workspace-context";
import { logout } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import { parseResourcePath } from "@/lib/workspace-path";

const primaryLinks = [
  { href: "/app", label: "Overview", icon: LayoutDashboard, exact: true },
  { href: "/app/clients", label: "Clients", icon: FolderKanban },
  { href: "/app/notifications", label: "Notifications", icon: Bell },
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
  const { user, tenant, tenants, permissionKeys = [], selectedTenantId, setSelectedTenantId, tenantFromRoute } = useWorkspace();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const resourcePath = parseResourcePath(pathname);

  async function signOut() {
    await logout();
    queryClient.clear();
    router.replace("/login");
    router.refresh();
  }

  const canManageAccess = user.is_superuser || user.tenant_role === "ADMIN" || permissionKeys.some((key) => key === "tenant.manage_users" || key === "tenant.manage_roles" || key === "tenant.manage_groups");
  const canViewAudit = user.is_superuser || user.tenant_role === "ADMIN" || permissionKeys.includes("tenant.view_audit");
  const navigation = user.is_superuser
    ? [
        ...primaryLinks,
        { href: "/app/access", label: "Access control", icon: ShieldCheck },
        { href: "/app/audit-log", label: "Audit log", icon: ScrollText },
        { href: "/admin/tenants", label: "Tenants", icon: Building2 },
        { href: "/admin/agents", label: "Agents", icon: Bot },
        { href: "/admin/skills", label: "Skills", icon: Wrench },
      ]
    : [
        ...primaryLinks,
        ...(canManageAccess ? [{ href: "/app/access", label: "Access control", icon: ShieldCheck }] : []),
        ...(canViewAudit ? [{ href: "/app/audit-log", label: "Audit log", icon: ScrollText }] : []),
      ];

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
        {resourcePath ? (
          // Inside a matter or collection the tenant card opens the workspace links, one level above "← client".
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                aria-label={`${tenant.name}. Workspace menu`}
                title={collapsed ? `${tenant.name} · Workspace` : "Workspace"}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg border border-sidebar-border bg-card text-left outline-none transition hover:border-input focus-visible:ring-2 focus-visible:ring-ring",
                  collapsed ? "size-10 justify-center p-0" : "px-3 py-2",
                )}
              >
                {collapsed ? (
                  <span className="font-bold text-primary">{tenant.name.slice(0, 1).toUpperCase()}</span>
                ) : (
                  <>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-semibold">{tenant.name}</span>
                      <span className="block truncate text-xs text-muted-foreground">{tenant.slug}</span>
                    </span>
                    <LayoutGrid className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                  </>
                )}
              </button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Portal>
              <DropdownMenu.Content side="bottom" align="start" className="z-50 mt-1 min-w-52 rounded-lg border bg-popover p-1 text-popover-foreground shadow-xl">
                {navigation.map(({ href, label, icon: Icon }) => (
                  <DropdownMenu.Item key={href} asChild className="flex cursor-default items-center gap-2 rounded-md px-3 py-2 text-sm outline-none focus:bg-muted">
                    <Link href={href} onClick={() => setMobileOpen(false)}><Icon className="size-4" aria-hidden="true" />{label}</Link>
                  </DropdownMenu.Item>
                ))}
              </DropdownMenu.Content>
            </DropdownMenu.Portal>
          </DropdownMenu.Root>
        ) : collapsed ? (
          <div className="grid size-10 place-items-center rounded-lg bg-primary/10 font-bold text-primary" title={tenant.name}>
            {tenant.name.slice(0, 1).toUpperCase()}
          </div>
        ) : user.is_superuser && tenants.length > 1 && !tenantFromRoute ? (
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

      {resourcePath?.kind === "matter" ? (
        <MatterSidebarNav clientId={resourcePath.clientId} matterId={resourcePath.matterId} collapsed={collapsed} onNavigate={() => setMobileOpen(false)} />
      ) : resourcePath?.kind === "collection" ? (
        <CollectionSidebarNav clientId={resourcePath.clientId} collectionId={resourcePath.collectionId} collapsed={collapsed} onNavigate={() => setMobileOpen(false)} />
      ) : (
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
      )}
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
          <NotificationBell />
          <ThemeToggle />
          <AccountMenu displayName={user.display_name} email={user.email} onSignOut={() => void signOut()} />
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

/** The signed-in user's menu at the top right of every workspace page. */
function AccountMenu({ displayName, email, onSignOut }: { displayName: string; email: string; onSignOut: () => void }) {
  const initials = displayName.split(" ").map((part) => part[0]).join("").slice(0, 2).toUpperCase();
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={`Account: ${displayName}`}
          className="flex shrink-0 items-center gap-2 rounded-full p-1 outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring md:rounded-lg md:pr-2"
        >
          <span aria-hidden="true" className="grid size-8 shrink-0 place-items-center rounded-full bg-primary text-xs font-bold text-primary-foreground">{initials}</span>
          <span className="hidden max-w-40 truncate text-sm font-semibold md:block">{displayName}</span>
          <ChevronDown aria-hidden="true" className="hidden size-4 text-muted-foreground md:block" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content side="bottom" align="end" sideOffset={6} className="z-50 min-w-56 rounded-lg border bg-popover p-1 text-popover-foreground shadow-xl">
          <div className="px-3 py-2">
            <p className="truncate text-sm font-semibold">{displayName}</p>
            <p className="truncate text-xs text-muted-foreground">{email}</p>
          </div>
          <DropdownMenu.Separator className="my-1 h-px bg-border" />
          <DropdownMenu.Item onSelect={onSignOut} className="flex cursor-default items-center gap-2 rounded-md px-3 py-2 text-sm text-destructive outline-none focus:bg-muted">
            <LogOut className="size-4" aria-hidden="true" /> Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
