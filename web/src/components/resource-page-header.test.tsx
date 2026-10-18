import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ResourcePageHeader } from "@/components/resource-page-header";

afterEach(() => cleanup());

describe("ResourcePageHeader", () => {
  it("shows only the breadcrumb and resource title", () => {
    render(
      <ResourcePageHeader
        breadcrumbs={<><a href="/clients">Clients</a><span>Example</span></>}
        title="Example resource"
      />,
    );

    const header = screen.getByRole("banner", { name: "Example resource header" });
    expect(within(header).getByRole("navigation", { name: "Breadcrumb" })).toBeVisible();
    expect(within(header).getByRole("heading", { name: "Example resource" })).toBeVisible();
    expect(within(header).queryByRole("button")).not.toBeInTheDocument();
  });

  it("places leading content immediately left of the title", () => {
    render(
      <ResourcePageHeader
        breadcrumbs={<><a href="/clients">Clients</a><span>Example</span></>}
        title="Example resource"
        leading={<><button type="button">Help</button><span>open</span></>}
      />,
    );

    const header = screen.getByRole("banner", { name: "Example resource header" });
    const heading = within(header).getByRole("heading", { name: "Example resource" });
    const help = within(header).getByRole("button", { name: "Help" });
    // Same row as the title, and before it.
    expect(heading.parentElement).toContainElement(help);
    expect(help.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(header).getByText("open").compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("replaces the visible title with an action when provided", () => {
    render(
      <ResourcePageHeader
        breadcrumbs={<><a href="/clients">Clients</a><span>Example</span></>}
        title="Example resource"
        action={<a href="/review">Search &amp; Review</a>}
      />,
    );

    const header = screen.getByRole("banner", { name: "Example resource header" });
    expect(within(header).getByRole("link", { name: "Search & Review" })).toHaveAttribute("href", "/review");
    expect(within(header).queryByRole("heading", { name: "Example resource" })).not.toBeInTheDocument();
  });
});
