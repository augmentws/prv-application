import type { NotificationRead } from "@/generated/models";

function stringParam(notification: NotificationRead, key: string) {
  const value = notification.primary_route_params[key];
  return typeof value === "string" && value ? value : null;
}

export function notificationHref(notification: NotificationRead) {
  const clientId = stringParam(notification, "client_id");
  const matterId = stringParam(notification, "matter_id");
  if (notification.primary_route_name === "MATTER_JOBS" && clientId && matterId) {
    return `/app/clients/${clientId}/matters/${matterId}/jobs`;
  }
  if (notification.primary_route_name === "AGENT_CONVERSATION" && clientId && matterId) {
    const workflowType = stringParam(notification, "workflow_type");
    if (workflowType === "MATTER_DEFINITION_SETUP") {
      return `/app/clients/${clientId}/matters/${matterId}/definition`;
    }
    const batchId = stringParam(notification, "review_batch_id");
    const params = new URLSearchParams();
    if (batchId) params.set("batch", batchId);
    return `/review/matters/${matterId}${params.size ? `?${params.toString()}` : ""}`;
  }
  return "/app/notifications";
}

export function notificationContext(notification: NotificationRead) {
  if (notification.matter_name && notification.client_name) {
    return `${notification.client_name} · ${notification.matter_name}`;
  }
  return notification.matter_name ?? notification.client_name ?? "Workspace";
}
