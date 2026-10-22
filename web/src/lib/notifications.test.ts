import { describe, expect, it } from "vitest";

import type { NotificationRead } from "@/generated/models";
import { notificationHref } from "@/lib/notifications";

describe("notificationHref", () => {
  it("routes a Review Program decision to its program and action", () => {
    expect(notificationHref({
      primary_route_name: "REVIEW_PROGRAM",
      primary_route_params: {
        client_id: "client-1",
        matter_id: "matter-1",
        program_id: "program-1",
        run_id: "run-1",
        action_id: "action-1",
      },
    } as unknown as NotificationRead)).toBe(
      "/app/clients/client-1/matters/matter-1/review-programs?program=program-1&action=action-1",
    );
  });
});
