import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CollectionProcessingPanel } from "@/components/collection-processing-panel";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

const customRule = {
  id: "remove-footer",
  name: "Remove footer",
  description: null,
  action: "REMOVE_LINE" as const,
  pattern: "^CONFIDENTIAL FOOTER$",
  end_pattern: null,
  replacement: "",
  case_sensitive: false,
  enabled: true,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CollectionProcessingPanel collectionId="collection-1" />
    </QueryClientProvider>,
  );
}

describe("CollectionProcessingPanel", () => {
  it("can exclude a rule from a test and shows the rules applied to each processed preview", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/collections/collection-1/text-processing/profile" && init?.method === "PUT") {
        const request = JSON.parse(init.body as string);
        return {
          collection_id: "collection-1",
          processor_version: "collection-text-v7",
          revision: 2,
          default_rules: [],
          rules: request.rules,
          active_run_id: null,
          updated_at: "2026-09-22T12:10:00Z",
        } as never;
      }
      if (path === "/v1/collections/collection-1/text-processing/profile") return {
        collection_id: "collection-1",
        processor_version: "collection-text-v7",
        revision: 1,
        default_rules: [{
          id: "system-attachment-placeholder",
          name: "Attachment placeholder",
          description: "Removes generated attachment lines.",
          action: "REMOVE_LINE",
          match_description: "Standalone attachment placeholder lines.",
          match_pattern: "(?im)^\\s*-\\s*(?:winmail\\.dat|smime\\.p7s)\\s*$",
          stop_pattern: "End of the matching line",
        }],
        rules: [customRule],
        active_run_id: null,
        updated_at: "2026-09-22T12:00:00Z",
      } as never;
      if (path === "/v1/collections/collection-1/search?limit=25&offset=0") return {
        items: [
          { id: "item-1", original_filename: "message.txt" },
          { id: "item-2", original_filename: "unchanged.txt" },
        ],
        total: 2,
      } as never;
      if (path === "/v1/collections/collection-1/text-processing/runs" && init?.method === "POST") return {
        id: "run-1",
        collection_id: "collection-1",
        status: "QUEUED",
        processor_version: "collection-text-v7",
        profile_revision: 1,
        rules_snapshot: [],
        disabled_rule_ids: ["remove-footer"],
        configuration_hash: "a".repeat(64),
        total_count: 0,
        processed_count: 0,
        created_count: 0,
        reused_count: 0,
        skipped_count: 0,
        failed_count: 0,
        error_message: null,
        created_at: "2026-09-22T12:05:00Z",
      } as never;
      if (path === "/v1/collections/collection-1/text-processing/runs") return [] as never;
      if (path === "/v1/agent-packages?scope_type=COLLECTION&scope_id=collection-1") return [{
        id: "cleaner-agent-1",
        key: "document_cleaner",
        name: "Document Cleaner Agent",
        description: "Builds text-cleaning rules.",
        version: {
          usage_instructions: "Select test documents and describe the cleanup.",
          scope_types: ["COLLECTION"],
        },
      }] as never;
      if (path === "/v1/agents/cleaner-agent-1:invoke" && init?.method === "POST") return {
        agent_id: "cleaner-agent-1",
        agent_version_id: "version-1",
        version: 1,
        output: {
          status: "PROPOSAL",
          clarifying_question: null,
          explanation: "Removes the exact recurring confidentiality footer line.",
          rule: {
            id: "remove-confidential-footer",
            name: "Remove confidentiality footer",
            description: "Removes the recurring confidentiality footer.",
            action: "REMOVE_LINE",
            pattern: "^CONFIDENTIAL FOOTER$",
            end_pattern: null,
            replacement: "",
            case_sensitive: false,
            enabled: true,
          },
          replace_rule_id: null,
        },
      } as never;
      if (path === "/v1/collections/collection-1/text-processing:test" && init?.method === "POST") return {
        processor_version: "collection-text-v7",
        items: [{
          item_id: "item-1",
          filename: "message.txt",
          source_role: "NATIVE",
          original_text: "Useful line\nCONFIDENTIAL FOOTER",
          normalized_text: "Useful line",
          original_char_count: 31,
          normalized_char_count: 11,
          changes: [{ rule_id: "remove-footer", rule_name: "Remove footer", match_count: 1 }],
          warnings: [],
        }, {
          item_id: "item-2",
          filename: "unchanged.txt",
          source_role: "NATIVE",
          original_text: "Useful line",
          normalized_text: "Useful line",
          original_char_count: 11,
          normalized_char_count: 11,
          changes: [],
          warnings: [],
        }],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByRole("link", { name: "Document Cleaner help" })).toHaveAttribute("href", "/docs/document-cleaner");

    const testRules = screen.getByRole("group", { name: "Rules included in test" });
    expect(screen.getByText("Pattern")).toBeInTheDocument();
    expect(screen.getByText("Stops at")).toBeInTheDocument();
    expect(screen.getByText("End of the matching line")).toBeInTheDocument();
    await user.click(within(testRules).getByRole("checkbox", { name: /Attachment placeholder/ }));
    await user.click(screen.getByRole("button", { name: "Select all 2" }));
    expect(screen.getByRole("checkbox", { name: "message.txt" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "unchanged.txt" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Clear all" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Test 2" }));

    await waitFor(() => {
      const request = vi.mocked(coreApi).mock.calls.find(([path]) => path === "/v1/collections/collection-1/text-processing:test");
      expect(request).toBeDefined();
      expect(JSON.parse(request?.[1]?.body as string)).toMatchObject({
        item_ids: ["item-1", "item-2"],
        disabled_rule_ids: ["system-attachment-placeholder"],
      });
    });

    const processed = await screen.findByRole("region", { name: "Processed preview for message.txt" });
    expect(within(processed).getByLabelText("Rule hits")).toBeInTheDocument();
    expect(within(processed).getByText(/Remove footer · 1 match/)).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Processed preview for unchanged.txt" })).getByText("No rule hits")).toBeInTheDocument();

    const runRules = screen.getByRole("group", { name: "Rules included in full collection run" });
    await user.click(within(runRules).getByRole("checkbox", { name: /Remove footer/ }));
    await user.click(screen.getByRole("button", { name: "Run full collection" }));

    await waitFor(() => {
      const request = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/collections/collection-1/text-processing/runs" && init?.method === "POST");
      expect(request).toBeDefined();
      expect(JSON.parse(request?.[1]?.body as string)).toEqual({
        enabled_rule_ids: ["system-attachment-placeholder"],
      });
    });

    await user.click(screen.getByRole("button", { name: "Add rule" }));
    expect(screen.getByText("Complete or turn off 1 incomplete rule before testing.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test 2" })).toBeDisabled();
    await user.click(within(testRules).getByRole("checkbox", { name: /New rule/ }));
    expect(screen.queryByText("Complete or turn off 1 incomplete rule before testing.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Test 2" }));

    await waitFor(() => {
      const requests = vi.mocked(coreApi).mock.calls.filter(([path]) => path === "/v1/collections/collection-1/text-processing:test");
      expect(requests).toHaveLength(2);
      expect(JSON.parse(requests.at(-1)?.[1]?.body as string).rules).toEqual([customRule]);
    });

    await user.click(screen.getByRole("button", { name: "Delete New rule" }));
    await user.click(screen.getByRole("button", { name: "Test 2" }));
    await user.click(await screen.findByRole("checkbox", { name: "Use message.txt in rule builder" }));
    await user.click(screen.getByRole("button", { name: "Build/improve rule (1)" }));
    await user.type(screen.getByLabelText("What should be cleaned up?"), "Remove the recurring confidentiality footer.");
    await user.click(screen.getByRole("button", { name: "Ask agent" }));
    expect(await screen.findByText("Remove confidentiality footer")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save proposed rule" }));

    await waitFor(() => {
      const invocation = vi.mocked(coreApi).mock.calls.find(([path]) => path === "/v1/agents/cleaner-agent-1:invoke");
      expect(invocation).toBeDefined();
      expect(JSON.parse(invocation?.[1]?.body as string)).toMatchObject({
        scope: { type: "COLLECTION", id: "collection-1" },
        input: {
          instruction: "Remove the recurring confidentiality footer.",
          documents: [{ item_id: "item-1", filename: "message.txt" }],
        },
      });
      const profileSave = vi.mocked(coreApi).mock.calls.find(([path, request]) => path === "/v1/collections/collection-1/text-processing/profile" && request?.method === "PUT");
      expect(JSON.parse(profileSave?.[1]?.body as string).rules).toHaveLength(2);
    });
  });
});
