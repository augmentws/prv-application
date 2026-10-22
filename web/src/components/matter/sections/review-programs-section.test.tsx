import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReviewProgramsSection } from "@/components/matter/sections/review-programs-section";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("@/components/matter/matter-context", () => ({
  useMatter: () => ({ clientId: "client-1", matterId: "matter-1", permissionKeys: new Set(["review_program.view", "review_program.manage"]) }),
}));
const navigation = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/app/clients/client-1/matters/matter-1/review-programs",
  useRouter: () => ({ replace: navigation.replace }),
  useSearchParams: () => new URLSearchParams("program=program-1&action=action-1"),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><ReviewProgramsSection /></QueryClientProvider>);
}

describe("ReviewProgramsSection", () => {
  it("configures a new Matter Definition through the guided setup action", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW",
        version: 1,
        schema_version: 1,
        content_hash: "a".repeat(64),
        name: "First Pass Review",
        description: "Guided review",
        workflow_type: "FIRST_PASS_REVIEW",
        phases: [],
        stages: [{ key: "MATTER_DEFINITION", name: "Matter definition", phase_key: "MATTER_DEFINITION", ordinal: 1, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/guidance") return [] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        template_key: "FIRST_PASS_REVIEW",
        template_version: 1,
        template_content_hash: "a".repeat(64),
        key: "standard-first-pass",
        name: "Standard first pass",
        description: "Responsive review",
        status: "ACTIVE",
        current_run_id: "run-1",
        current_version: { version: 1 },
        created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1",
        program_id: "program-1",
        status: "WAITING_FOR_USER",
        current_stage: "MATTER_DEFINITION",
        current_stage_ordinal: 1,
        created_at: "2026-10-21T00:00:00Z",
        failure_retriable: false,
        failure_summary: null,
        stage_attempts: [{ stage_key: "MATTER_DEFINITION", status: "WAITING" }],
        actions: [{
          id: "action-1",
          action_type: "MATTER_DEFINITION_SETUP",
          stage_key: "MATTER_DEFINITION",
          status: "OPEN",
          title: "Set up the Matter Definition",
          explanation: "Create, clone, or adopt a definition before assessment.",
          preview_hash: "b".repeat(64),
          allowed_choices: [
            { key: "CREATE", label: "Create definition", consequence: "Create a definition." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/definition-setup") && options?.method === "POST") return {
        id: "run-1",
        program_id: "program-1",
        status: "RUNNING",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    await userEvent.type(await screen.findByLabelText("Definition name"), "Hurricane response");
    await userEvent.type(screen.getByLabelText("Matter Definition"), "Review hurricane disruption communications.");
    await userEvent.click(screen.getByRole("button", { name: "Save and assess" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/definition-setup",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/definition-setup"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual(expect.objectContaining({
      preview_hash: "b".repeat(64),
      mode: "CREATE",
      definition_key: "hurricane-response",
      name: "Hurricane response",
      content_markdown: "Review hurricane disruption communications.",
      assessment_target_document_count: 500,
      assessment_control_sample_size: 0,
      assessment_use_batching: true,
      acknowledge_large_run_warning: false,
    }));
  });

  it("shows the current gate and resolves it through the coordinator endpoint", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW",
        version: 1,
        schema_version: 1,
        content_hash: "a".repeat(64),
        name: "First Pass Review",
        description: "Guided review",
        workflow_type: "FIRST_PASS_REVIEW",
        phases: [],
        stages: [{ key: "DEFINITION_ACCEPTANCE", name: "Definition acceptance", phase_key: "MATTER_DEFINITION", ordinal: 4, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        template_key: "FIRST_PASS_REVIEW",
        template_version: 1,
        template_content_hash: "a".repeat(64),
        key: "standard-first-pass",
        name: "Standard first pass",
        description: "Responsive review",
        status: "ACTIVE",
        current_run_id: "run-1",
        current_version: { version: 1 },
        created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1",
        program_id: "program-1",
        status: "WAITING_FOR_USER",
        current_stage: "DEFINITION_ACCEPTANCE",
        current_stage_ordinal: 4,
        created_at: "2026-10-21T00:00:00Z",
        failure_retriable: false,
        failure_summary: null,
        stage_attempts: [{ stage_key: "DEFINITION_ACCEPTANCE", status: "WAITING" }],
        actions: [{
          id: "action-1",
          status: "OPEN",
          title: "Review Definition acceptance",
          explanation: "Confirm the definition before Q&A setup.",
          preview_hash: "b".repeat(64),
          allowed_choices: [
            { key: "CONTINUE", label: "Continue to Q&A task setup", consequence: "Advance the program." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1",
        program_id: "program-1",
        status: "RUNNING",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Confirm the definition before Q&A setup.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue to Q&A task setup" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ preview_hash: "b".repeat(64), choice_key: "CONTINUE" });
  });
});
