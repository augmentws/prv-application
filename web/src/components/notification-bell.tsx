"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, CheckCircle2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { NotificationCounts, NotificationMutationResult, NotificationPage } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { notificationContext, notificationHref } from "@/lib/notifications";
import { cn } from "@/lib/utils";

export function NotificationBell() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const counts = useQuery({
    queryKey: ["notification-counts"],
    queryFn: () => coreApi<NotificationCounts>("/v1/notifications/counts"),
    refetchInterval: 15_000,
  });
  const recent = useQuery({
    queryKey: ["notifications", "recent"],
    queryFn: () => coreApi<NotificationPage>("/v1/notifications?limit=5"),
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  });
  const markRead = useMutation({
    mutationFn: (id: string) => coreApi<NotificationMutationResult>(`/v1/notifications/${id}/read`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notification-counts"] });
      void queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
  });
  const unread = counts.data?.unread ?? 0;

  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
        <Button type="button" variant="ghost" size="icon" className="relative" aria-label={`Notifications${unread ? `, ${unread} unread` : ""}`}>
          <Bell aria-hidden="true" />
          {unread ? (
            <span className="absolute right-0.5 top-0.5 grid min-w-4 place-items-center rounded-full bg-destructive px-1 text-[10px] font-bold leading-4 text-destructive-foreground">
              {unread > 99 ? "99+" : unread}
            </span>
          ) : null}
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content side="bottom" align="end" sideOffset={6} className="z-50 w-[min(24rem,calc(100vw-2rem))] rounded-xl border bg-popover p-1 text-popover-foreground shadow-xl">
          <div className="flex items-center justify-between gap-3 px-3 py-2">
            <div>
              <p className="text-sm font-semibold">Notifications</p>
              <p className="text-xs text-muted-foreground">
                {counts.data?.action_required ? `${counts.data.action_required} need action` : unread ? `${unread} unread` : "You're caught up"}
              </p>
            </div>
            <CheckCircle2 className="size-4 text-muted-foreground" aria-hidden="true" />
          </div>
          <DropdownMenu.Separator className="h-px bg-border" />
          <div className="max-h-96 overflow-y-auto py-1">
            {recent.isPending ? <p className="px-3 py-6 text-center text-sm text-muted-foreground">Loading…</p>
              : recent.error ? <p className="px-3 py-6 text-center text-sm text-destructive">Notifications could not be loaded.</p>
              : recent.data?.items.length ? recent.data.items.map((notification) => (
                <DropdownMenu.Item key={notification.id} asChild>
                  <Link
                    href={notificationHref(notification)}
                    onClick={() => { if (!notification.read_at) markRead.mutate(notification.id); }}
                    className="block cursor-default rounded-lg px-3 py-2.5 outline-none focus:bg-muted"
                  >
                    <span className="flex items-start gap-2">
                      <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", notification.read_at ? "bg-transparent" : notification.kind === "ACTION_REQUIRED" && !notification.resolved_at ? "bg-warning" : "bg-primary")} />
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-semibold">{notification.title}</span>
                        <span className="mt-0.5 line-clamp-2 block text-xs leading-5 text-muted-foreground">{notification.summary}</span>
                        <span className="mt-1 block truncate text-[11px] text-muted-foreground">{notificationContext(notification)}</span>
                      </span>
                    </span>
                  </Link>
                </DropdownMenu.Item>
              )) : <p className="px-3 py-6 text-center text-sm text-muted-foreground">No notifications yet.</p>}
          </div>
          <DropdownMenu.Separator className="h-px bg-border" />
          <DropdownMenu.Item asChild>
            <Link href="/app/notifications" className="block cursor-default rounded-lg px-3 py-2 text-center text-sm font-semibold outline-none focus:bg-muted">View all notifications</Link>
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
