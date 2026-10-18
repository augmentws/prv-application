import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CustodiansSection } from "@/components/collection/sections/custodians-section";
import { DocumentsSection } from "@/components/collection/sections/documents-section";
import { FileDatesSection } from "@/components/collection/sections/file-dates-section";
import { COLLECTION_BASE_PATH as base, renderInCollection } from "@/components/collection/test-utils";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn(), coreApiContent: vi.fn(async () => { throw new Error("Content is not loaded in these tests"); }) }));
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
const navigation = vi.hoisted(() => ({ pathname: "/app/clients/client-1/collections/collection-1", params: new URLSearchParams(), push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useRouter: () => ({ push: navigation.push, replace: navigation.replace }),
  useSearchParams: () => navigation.params,
}));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

const item = {
  id: "item-1",
  original_filename: "memo.pdf",
  original_extension: ".pdf",
  source_item_id: "src-1",
  record_type: "FILE",
  email: null,
  original_source_path: "/share/memo.pdf",
  custodian_ids: ["cust-1"],
  file_date: "2001-01-05T00:00:00Z",
  native_artifact: { byte_length: 2048, media_type: "application/pdf" },
  processing_status: "PROCESSED",
};

beforeEach(() => {
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "TREC" } as never;
    if (path === "/v1/collections/collection-1") return { id: "collection-1", client_id: "client-1", tenant_id: "tenant-1", name: "Enron mailboxes", status: "ACTIVE" } as never;
    if (path === "/v1/authorization/effective?resource_type=COLLECTION&resource_id=collection-1") return { permission_keys: [] } as never;
    if (path === "/v1/clients/client-1/matters") return [{ id: "matter-1", name: "Trec-JBush" }] as never;
    if (path === "/v1/clients/client-1/custodians") return [{ id: "cust-1", display_name: "Jeb Bush", email_addresses: ["jeb@example.com"], external_reference: null, status: "ACTIVE" }] as never;
    if (String(path).startsWith("/v1/collections/collection-1/search?")) return { items: [item], total: 1 } as never;
    if (String(path).startsWith("/v1/collections/collection-1/search/facets/file_extensions")) return [{ value: "pdf", count: 1 }, { value: "docx", count: 3 }] as never;
    if (path === "/v1/collections/collection-1/custodians") return [{ custodian_id: "cust-1", item_count: 7 }] as never;
    if (path === "/v1/collections/collection-1/date-histogram?interval=month") return { interval: "month", buckets: [{ start: "2001-01-01T00:00:00Z", count: 4 }], missing_count: 2 } as never;
    if (path === "/v1/matters/matter-1/document-imports" && init?.method === "POST") return { id: "job-1" } as never;
    throw new Error(`Unexpected API request: ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.pathname = base;
  navigation.params = new URLSearchParams();
});

function postedImport() {
  const call = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/matters/matter-1/document-imports" && init?.method === "POST");
  return JSON.parse(String(call?.[1]?.body));
}

async function addToMatter(user: ReturnType<typeof userEvent.setup>, trigger: string) {
  // The button stays disabled until the client's matters (and, for Documents, the results) have loaded.
  const button = screen.getByRole("button", { name: trigger });
  await waitFor(() => expect(button).toBeEnabled());
  await user.click(button);
  await user.click(await screen.findByRole("combobox", { name: "Destination matter" }));
  await user.click(await screen.findByRole("option", { name: "Trec-JBush" }));
  await user.click(screen.getByRole("button", { name: "Start job" }));
}

describe("DocumentsSection", () => {
  it("searches with the filters and page in the address", async () => {
    navigation.params = new URLSearchParams("q=memo&ext=pdf&offset=50");
    renderInCollection(<DocumentsSection />);

    // Rows re-render as facet and custodian data arrive, so check the request rather than hold on to a row element.
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith("/v1/collections/collection-1/search?q=memo&extension=pdf&limit=50&offset=50"));
    expect(await screen.findByRole("button", { name: "memo.pdf" })).toHaveTextContent("memo.pdf");
    expect(screen.getByRole("textbox", { name: "Search collection" })).toHaveValue("memo");
  });

  it("pushes a facet change to the address, back on the first page", async () => {
    navigation.params = new URLSearchParams("q=memo&ext=pdf&offset=50");
    renderInCollection(<DocumentsSection />);

    const details = (await screen.findByText("File extension")).closest("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    fireEvent.click(await screen.findByRole("checkbox", { name: /\.docx/ }));

    expect(navigation.push).toHaveBeenCalledWith(`${base}?q=memo&ext=pdf&ext=docx`, { scroll: false });
  });

  it("opens a document by replacing the address entry", async () => {
    const user = userEvent.setup();
    renderInCollection(<DocumentsSection />);

    await user.click(await screen.findByRole("button", { name: "memo.pdf" }));
    expect(navigation.replace).toHaveBeenCalledWith(`${base}?item=item-1`, { scroll: false });
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it("shows the document named in the address", async () => {
    navigation.params = new URLSearchParams("item=item-1");
    renderInCollection(<DocumentsSection />);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("adds exactly the filtered documents to a matter", async () => {
    navigation.params = new URLSearchParams("q=memo&from=2001-01-01&to=2001-01-31");
    const user = userEvent.setup();
    renderInCollection(<DocumentsSection />);
    await screen.findByRole("button", { name: "memo.pdf" });

    await addToMatter(user, "Add all matching");

    expect(postedImport()).toMatchObject({
      source_collection_id: "collection-1",
      selection: { mode: "QUERY", q: "memo", file_date_from: "2001-01-01T00:00:00.000Z", file_date_to: "2001-01-31T23:59:59.999Z" },
      selection_summary: "Current collection search and filters",
    });
    expect(navigation.push).toHaveBeenCalledWith("/app/clients/client-1/matters/matter-1/jobs/imports?job=job-1");
  });
});

describe("FileDatesSection", () => {
  it("opens Documents filtered to a clicked bucket", async () => {
    navigation.pathname = `${base}/file-dates`;
    renderInCollection(<FileDatesSection />);

    fireEvent.click(await screen.findByRole("button", { name: /January 2001: 4 documents/ }));

    expect(navigation.push).toHaveBeenCalledWith(`${base}?from=2001-01-01&to=2001-01-31`);
    expect(screen.getByText("2 documents do not have a file date.")).toBeInTheDocument();
  });
});

describe("CustodiansSection", () => {
  it("adds the selected custodians' documents to a matter", async () => {
    navigation.pathname = `${base}/custodians`;
    const user = userEvent.setup();
    renderInCollection(<CustodiansSection />);

    const row = (await screen.findByText("Jeb Bush")).closest("tr")!;
    expect(within(row).getByText("7")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Select Jeb Bush" }));
    await addToMatter(user, "Add documents to matter");

    expect(postedImport()).toMatchObject({ selection: { mode: "QUERY", custodian_ids: ["cust-1"] }, selection_summary: "1 selected custodian" });
  });
});

describe("CollectionShell", () => {
  it("names the section in the breadcrumb", async () => {
    navigation.pathname = `${base}/custodians`;
    renderInCollection(<div>Section</div>);

    const breadcrumb = await screen.findByRole("navigation", { name: "Breadcrumb" });
    expect(within(breadcrumb).getByRole("link", { name: "Enron mailboxes" })).toHaveAttribute("href", base);
    expect(within(breadcrumb).getByText("Custodians")).toHaveAttribute("aria-current", "page");
    // Help and status sit just left of the collection name.
    const heading = screen.getByRole("heading", { name: "Enron mailboxes" });
    expect(heading.parentElement).toContainElement(screen.getByRole("link", { name: "Collections help" }));
    // Collections are deleted from the client page, not here.
    expect(screen.queryByRole("button", { name: /Delete Collection/ })).not.toBeInTheDocument();
  });

  it("leaves detail pages to draw their own header", async () => {
    navigation.pathname = `${base}/unknown`;
    renderInCollection(<div>Detail</div>);

    expect(await screen.findByText("Detail")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Breadcrumb" })).not.toBeInTheDocument();
  });
});
