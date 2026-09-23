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
});
