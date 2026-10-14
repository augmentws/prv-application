import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClassificationPanel } from "@/components/classification-panel";
import { ApiError, coreApi } from "@/lib/api-client";

const push = vi.fn();

vi.mock("@/lib/api-client", () => ({
  ApiError: class ApiError extends Error {
    constructor(message: string, public readonly status: number) {
      super(message);
    }
  },
  coreApi: vi.fn(),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const responsiveness = {
  id: "field-responsive",
  display_name: "Responsiveness",
  key: "responsiveness",
  type: "ENUM",
  cardinality: "SINGLE",
  status: "ACTIVE",
  value_source: "ASSERTED",
  facetable: true,
} as never;

const customNotes = {
  id: "field-notes",
  display_name: "Case assessment",
  key: "case_assessment",
  type: "LONG_TEXT",
  cardinality: "SINGLE",
  status: "ACTIVE",
  value_source: "ASSERTED",
} as never;

const importedSubject = {
  id: "field-subject",
  display_name: "Email subject",
  key: "email_subject",
  type: "TEXT",
  cardinality: "SINGLE",
  status: "ACTIVE",
  value_source: "IMPORTED",
} as never;

const hierarchicalText = {
  id: "field-hierarchy",
  display_name: "Issue path",
  key: "issue_path",
  type: "TEXT",
  cardinality: "SINGLE",
  status: "ACTIVE",
  value_source: "ASSERTED",
  searchable: true,
  facetable: true,
  hierarchy_separator: "/",
} as never;

const completedEmbeddingJob = {
  id: "embedding-1",
  status: "COMPLETED",
  embedding_model: "text-embedding-3-small",
  embedding_model_revision: "2026-01",
  embedding_dimensions: 1536,
  chunk_count: 120,
} as never;

const recoveredEmbeddingJob = {
  id: "embedding-recovered",
  status: "CANCELED",
  embedding_model: "voyage-4-nano",
  embedding_model_revision: "2026-01",
  embedding_dimensions: 1024,
  total_count: 290_099,
  processed_count: 290_099,
  chunk_count: 446_520,
} as never;

function renderPanel(options?: { definitions?: never[]; embeddingJobs?: never[] }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ClassificationPanel
        clientId="client-1"
        matterId="matter-1"
        definitions={options?.definitions ?? [responsiveness, customNotes, importedSubject]}
        reviewBatches={[]}
        embeddingJobs={options?.embeddingJobs ?? []}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ClassificationPanel", () => {
  it("starts with asserted coding fields and excludes imported text metadata", async () => {
    vi.mocked(coreApi).mockResolvedValue([] as never);

    renderPanel();

    expect(await screen.findByText("Responsiveness")).toBeInTheDocument();
    expect(screen.getByText("Case assessment")).toBeInTheDocument();
    expect(screen.queryByText("Email subject")).not.toBeInTheDocument();
    expect(screen.getAllByText("0 models")).toHaveLength(2);
  });

  it("does not label a model activated until it has an active version", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/classification-models") return [{
        id: "model-1",
        name: "Responsiveness model",
        status: "ACTIVE",
        active_model_version_id: null,
        target_metadata_definition_id: "field-responsive",
        current_configuration: { classification_strategy: "BINARY" },
        created_at: "2026-10-05T12:00:00Z",
      }] as never;
      if (path.endsWith("/facets/responsiveness/values")) return {
        field: "responsiveness",
        total_count: 30,
        missing_count: 5,
        values: [{ value: "responsive", count: 25 }],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    renderPanel({ definitions: [responsiveness] });
    await user.click(await screen.findByRole("button", { name: /Responsiveness/ }));

    expect(await screen.findByText("Not activated")).toBeInTheDocument();
    expect(screen.queryByText("ACTIVE")).not.toBeInTheDocument();
  });

  it("reuses a canceled embedding run when all documents were processed", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      if (path.endsWith("/facets/responsiveness/values")) return {
        field: "responsiveness",
        total_count: 30,
        missing_count: 5,
        values: [{ value: "responsive", count: 25 }],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    renderPanel({ definitions: [responsiveness], embeddingJobs: [recoveredEmbeddingJob] });
    await user.click(await screen.findByRole("button", { name: /Responsiveness/ }));

    expect(await screen.findByText(/Recovered completed output/)).toBeInTheDocument();
    expect(screen.getByText(/canceled after all 290,099 documents were processed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Prepare embeddings" })).not.toBeInTheDocument();
  });

  it("turns one Train action into a durable training job", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/matters/matter-1/classification-models" && !options) return [] as never;
      if (path === "/v1/matters/matter-1/classification-models" && options?.method === "POST") return {
        id: "model-1",
        matter_id: "matter-1",
        target_metadata_definition_id: "field-responsive",
        name: "Responsiveness model",
      } as never;
      if (path.endsWith("/facets/responsiveness/values")) return {
        field: "responsiveness",
        total_count: 30,
        missing_count: 5,
        values: [
          { value: "responsive", count: 15 },
          { value: "not_responsive", count: 10 },
        ],
      } as never;
      if (path.endsWith("/configurations")) return { id: "configuration-1" } as never;
      if (path.endsWith("/training-jobs")) return { created: true, training_job: { id: "job-1" } } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    renderPanel({ definitions: [responsiveness], embeddingJobs: [completedEmbeddingJob] });
    await user.click(await screen.findByRole("button", { name: /Responsiveness/ }));
    expect(await screen.findByLabelText("Documents per class")).toBeInTheDocument();
    expect(screen.getByText("25")).toBeInTheDocument();
    expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/facets/responsiveness/values",
      expect.objectContaining({
        body: expect.stringMatching(/"search":\{.*"size":50.*\},"query":null,"size":50/),
      }),
    );
    await user.click(screen.getByRole("button", { name: "Train model" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith(
      "/app/clients/client-1/matters/matter-1/classification-models/model-1",
    ));
    expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/classification-models/model-1/training-jobs",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("falls back to database coding only when a field cannot be faceted", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/matters/matter-1/classification-models" && !options) return [] as never;
      if (path.endsWith("/facets/case_assessment/values")) {
        throw new ApiError("Unknown or non-facetable field", 422);
      }
      if (path.endsWith("/classification-models/targets/field-notes/distribution")) return {
        population_count: 30,
        labeled_document_count: 8,
        unlabeled_document_count: 22,
        conflicting_document_count: 0,
        class_counts: [{ class_key: "needs_follow_up", label: "Needs follow-up", document_count: 8 }],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    renderPanel({ definitions: [customNotes] });
    await user.click(await screen.findByRole("button", { name: /Case assessment/ }));

    expect(await screen.findByLabelText("Documents per class")).toBeInTheDocument();
    expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/facets/case_assessment/values",
      expect.any(Object),
    );
    expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/classification-models/targets/field-notes/distribution",
      expect.any(Object),
    );
  });

  it("does not disguise other facet validation errors as non-facetable fields", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/matters/matter-1/classification-models" && !options) return [] as never;
      if (path.endsWith("/facets/responsiveness/values")) {
        throw new ApiError("Facet request validation failed", 422);
      }
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    renderPanel({ definitions: [responsiveness] });
    await user.click(await screen.findByRole("button", { name: /Responsiveness/ }));

    expect(await screen.findByText("The class distribution could not be loaded.")).toBeInTheDocument();
    expect(coreApi).not.toHaveBeenCalledWith(
      "/v1/matters/matter-1/classification-models/targets/field-responsive/distribution",
      expect.any(Object),
    );
  });

  it("trains hierarchical text at the selected hierarchy level", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/matters/matter-1/classification-models" && !options) return [] as never;
      if (path.endsWith("/classification-models/targets/field-hierarchy/distribution")) return {
        population_count: 30,
        labeled_document_count: 25,
        unlabeled_document_count: 5,
        conflicting_document_count: 0,
        class_counts: [{ class_key: "Projects/Alpha", label: "Projects/Alpha", document_count: 25 }],
      } as never;
      if (path === "/v1/matters/matter-1/classification-models" && options?.method === "POST") return {
        id: "model-hierarchy",
        target_metadata_definition_id: "field-hierarchy",
      } as never;
      if (path.endsWith("/configurations")) return { id: "configuration-hierarchy" } as never;
      if (path.endsWith("/training-jobs")) return { created: true, training_job: { id: "job-hierarchy" } } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    renderPanel({ definitions: [hierarchicalText], embeddingJobs: [completedEmbeddingJob] });
    await user.click(await screen.findByRole("button", { name: /Issue path/ }));
    const level = await screen.findByLabelText("Training hierarchy level");
    expect(level).toHaveAttribute("placeholder", "Full path");
    await user.type(level, "2");

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/classification-models/targets/field-hierarchy/distribution",
      expect.objectContaining({ body: expect.stringContaining('"hierarchy_level":2') }),
    ));
    expect(coreApi).not.toHaveBeenCalledWith(
      "/v1/matters/matter-1/facets/issue_path/values",
      expect.any(Object),
    );

    await user.click(screen.getByRole("button", { name: "Train model" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/classification-models/model-hierarchy/configurations",
      expect.objectContaining({ body: expect.stringContaining('"hierarchy_level":2') }),
    ));
  });
});
