import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FileText, Layers, LayoutDashboard } from "lucide-react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ResourceSidebarNav, type ResourceSidebarNavProps } from "@/components/resource-nav/resource-sidebar-nav";
import { type NavGroup, visibleNav } from "@/components/resource-nav/types";

vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

type Target = "home" | "items" | "setup";

const GROUPS: NavGroup<Target>[] = [
  { items: [{ label: "Home", icon: LayoutDashboard, target: "home" }] },
  { label: "Work", items: [{ label: "Items", icon: Layers, target: "items" }] },
  { label: "Setup", items: [{ label: "Settings", icon: FileText, target: "setup", permission: "thing.configure" }] },
];

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderNav(overrides: Partial<ResourceSidebarNavProps<Target>> = {}) {
  const props: ResourceSidebarNavProps<Target> = {
    label: "Thing",
    pluralLabel: "things",
    client: { id: "client-1", name: "TREC" },
    current: { id: "thing-1", name: "First thing" },
    siblings: { data: [{ id: "thing-1", name: "First thing" }, { id: "thing-2", name: "Second thing" }], isPending: false, error: null },
    groups: GROUPS,
    activeTarget: "home",
    hrefFor: (target) => `/things/thing-1/${target}`,
    onSwitch: vi.fn(),
    collapsed: false,
    onNavigate: vi.fn(),
    ...overrides,
  };
  render(<ResourceSidebarNav {...props} />);
  return props;
}

describe("visibleNav", () => {
  it("drops items without permission and the groups they leave empty", () => {
    expect(visibleNav(GROUPS, new Set()).map((group) => group.label)).toEqual([undefined, "Work"]);
    expect(visibleNav(GROUPS, new Set(["thing.configure"])).map((group) => group.label)).toEqual([undefined, "Work", "Setup"]);
  });
});

describe("ResourceSidebarNav", () => {
  it("links back to the client and to every section, in group order", () => {
    renderNav();

    expect(screen.getByRole("navigation", { name: "Thing" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "TREC" })).toHaveAttribute("href", "/app/clients/client-1");
    expect(within(screen.getByRole("group", { name: "Work" })).getByRole("link", { name: "Items" })).toHaveAttribute("href", "/things/thing-1/items");
    expect(screen.getAllByRole("link").map((link) => link.textContent)).toEqual(["TREC", "Home", "Items", "Settings"]);
  });

  it("marks only the active section current", () => {
    renderNav({ activeTarget: "items" });

    expect(screen.getByRole("link", { name: "Items" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");
  });

  it("switches to another resource but not to the current one", async () => {
    const user = userEvent.setup();
    const props = renderNav();

    await user.click(screen.getByRole("button", { name: "Thing: First thing. Switch thing" }));
    await user.click(await screen.findByRole("menuitem", { name: "First thing" }));
    expect(props.onSwitch).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Thing: First thing. Switch thing" }));
    await user.click(await screen.findByRole("menuitem", { name: "Second thing" }));
    expect(props.onSwitch).toHaveBeenCalledWith("thing-2");
  });

  it("shows loading and error rows in the switcher", async () => {
    const user = userEvent.setup();
    renderNav({ siblings: { isPending: false, error: new Error("boom") } });

    await user.click(screen.getByRole("button", { name: "Thing: First thing. Switch thing" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Things could not be loaded: boom");
  });

  it("reports a permissions error under the menu", () => {
    renderNav({ permissionsError: new Error("denied") });

    expect(screen.getByRole("alert")).toHaveTextContent("Thing permissions could not be loaded: denied");
  });

  it("keeps accessible names and drops group headings when collapsed", () => {
    renderNav({ collapsed: true });

    expect(screen.getByRole("link", { name: "Items" })).toHaveAttribute("title", "Items");
    expect(screen.getByRole("link", { name: "TREC" })).toHaveAttribute("title", "TREC");
    expect(screen.queryByText("Work")).not.toBeInTheDocument();
  });
});
