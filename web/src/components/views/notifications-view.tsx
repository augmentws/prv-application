"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Check, CheckCheck, ChevronLeft, ChevronRight, CircleAlert, CircleCheck, Info, X } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { PageHeader } from "@/components/page-header";
import { QueryError } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useWorkspace } from "@/components/workspace-context";
import type { NotificationMutationResult, NotificationPage, NotificationRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { notificationContext, notificationHref } from "@/lib/notifications";
import { cn } from "@/lib/utils";

type InboxState = "ALL" | "UNREAD" | "ACTION_REQUIRED";

function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function severityIcon(notification: NotificationRead) {
  if (notification.kind === "ACTION_REQUIRED" && !notification.resolved_at) return CircleAlert;
  if (notification.severity === "SUCCESS") return CircleCheck;
  if (notification.severity === "ERROR" || notification.severity === "WARNING") return CircleAlert;
  return Info;
}

export function NotificationsView() {
  const { tenant } = useWorkspace();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const [state, setState] = useState<InboxState>("ALL");
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null]);
  const cursor = cursorStack[cursorStack.length - 1];
  const matterId = searchParams.get("matter_id");
  const clientId = searchParams.get("client_id");
  const path = new URLSearchParams({ state, limit: "25" });
  if (cursor) path.set("cursor", cursor);
  if (matterId) path.set("matter_id", matterId);
  if (clientId) path.set("client_id", clientId);

  const notifications = useQuery({
    queryKey: ["notifications", state, cursor, matterId, clientId],
    queryFn: () => coreApi<NotificationPage>(`/v1/notifications?${path.toString()}`),
    refetchInterval: 15_000,
  });
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["notifications"] });
    void queryClient.invalidateQueries({ queryKey: ["notification-counts"] });
  };
  const markRead = useMutation({
    mutationFn: (id: string) => coreApi<NotificationMutationResult>(`/v1/notifications/${id}/read`, { method: "POST" }),
    onSuccess: refresh,
  });
  const dismiss = useMutation({
    mutationFn: (id: string) => coreApi<NotificationMutationResult>(`/v1/notifications/${id}/dismiss`, { method: "POST" }),
    onSuccess: refresh,
  });
  const markAllRead = useMutation({
    mutationFn: (ids: string[]) => coreApi<NotificationMutationResult>("/v1/notifications/mark-read", {
      method: "POST",
      body: JSON.stringify({ notification_ids: ids }),
    }),
    onSuccess: refresh,
  });
  const unreadIds = (notifications.data?.items ?? []).filter((item) => !item.read_at).map((item) => item.id);

  function selectState(next: InboxState) {
    setState(next);
    setCursorStack([null]);
  }

  return (
    <>
      <PageHeader
        eyebrow={tenant.name}
        title="Notifications"
        description="Review completed work, failures, and requests that need your attention."
        actions={unreadIds.length ? (
          <Button variant="outline" disabled={markAllRead.isPending} onClick={() => markAllRead.mutate(unreadIds)}><CheckCheck />Mark page read</Button>
        ) : undefined}
      />

      <div className="mb-5 flex flex-wrap gap-2" role="tablist" aria-label="Notification views">
        {(["ALL", "UNREAD", "ACTION_REQUIRED"] as const).map((value) => (
          <Button key={value} type="button" role="tab" variant={state === value ? "default" : "outline"} aria-selected={state === value} onClick={() => selectState(value)}>
            {value === "ALL" ? "All" : value === "UNREAD" ? "Unread" : "Needs action"}
          </Button>
        ))}
      </div>

      {notifications.isPending ? (
        <div className="grid min-h-52 place-items-center rounded-xl border bg-card text-sm text-muted-foreground"><Bell className="mb-2 size-5 animate-pulse" />Loading notifications…</div>
      ) : notifications.error ? <QueryError message={notifications.error.message} />
        : notifications.data?.items.length ? (
          <div className="space-y-3">
            {notifications.data.items.map((notification) => {
              const Icon = severityIcon(notification);
              const actionOpen = notification.kind === "ACTION_REQUIRED" && !notification.resolved_at;
              return (
                <Card key={notification.id} className={cn("p-4", !notification.read_at && "border-primary/35 bg-primary/[0.025]")}>
                  <div className="flex items-start gap-3">
                    <div className={cn("mt-0.5 grid size-9 shrink-0 place-items-center rounded-full", actionOpen ? "bg-warning/15 text-warning-foreground" : notification.severity === "ERROR" ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground")}>
                      <Icon className="size-4" aria-hidden="true" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="font-semibold">{notification.title}</h2>
                        {!notification.read_at ? <Badge variant="accent">Unread</Badge> : null}
                        {actionOpen ? <Badge variant="outline">Needs action</Badge> : null}
                        {notification.resolved_at ? <Badge variant="active">Resolved</Badge> : null}
                      </div>
                      <p className="mt-1 text-sm leading-6 text-muted-foreground">{notification.summary}</p>
                      <p className="mt-2 text-xs text-muted-foreground">{notificationContext(notification)} · {formatTimestamp(notification.occurred_at)}</p>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Button asChild size="sm">
                          <Link href={notificationHref(notification)} onClick={() => { if (!notification.read_at) markRead.mutate(notification.id); }}>
                            {actionOpen ? "Review request" : "Open"}
                          </Link>
                        </Button>
                        {!notification.read_at ? <Button size="sm" variant="outline" disabled={markRead.isPending} onClick={() => markRead.mutate(notification.id)}><Check />Mark read</Button> : null}
                        {!actionOpen ? <Button size="sm" variant="ghost" disabled={dismiss.isPending} onClick={() => dismiss.mutate(notification.id)}><X />Dismiss</Button> : null}
                      </div>
                    </div>
                  </div>
                </Card>
              );
            })}
          </div>
        ) : (
          <div className="grid min-h-52 place-items-center rounded-xl border border-dashed bg-card p-6 text-center">
            <div><CircleCheck className="mx-auto size-8 text-muted-foreground" /><h2 className="mt-3 font-semibold">Nothing here</h2><p className="mt-1 text-sm text-muted-foreground">There are no notifications in this view.</p></div>
          </div>
        )}

      <div className="mt-5 flex justify-end gap-2">
        <Button variant="outline" disabled={cursorStack.length === 1 || notifications.isFetching} onClick={() => setCursorStack((current) => current.slice(0, -1))}><ChevronLeft />Newer</Button>
        <Button variant="outline" disabled={!notifications.data?.next_cursor || notifications.isFetching} onClick={() => notifications.data?.next_cursor && setCursorStack((current) => [...current, notifications.data.next_cursor])}>Older<ChevronRight /></Button>
      </div>
    </>
  );
}
