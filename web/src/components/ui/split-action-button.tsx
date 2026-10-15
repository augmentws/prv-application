"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { ChevronDown } from "lucide-react";
import type { ReactElement, ReactNode } from "react";

import { Button, type ButtonProps } from "@/components/ui/button";

export interface SplitActionMenuItem {
  id: string;
  label: string;
  icon?: ReactNode;
  disabled?: boolean;
  destructive?: boolean;
  href?: string;
  target?: "_blank" | "_self";
  onSelect?: () => void;
}

export function SplitActionButton({
  label,
  primary,
  actions,
  variant = "default",
  size = "sm",
}: {
  label: string;
  primary: ReactElement;
  actions: SplitActionMenuItem[];
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
}) {
  return (
    <div
      className="inline-flex gap-0 overflow-hidden rounded-md shadow-sm focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 focus-within:ring-offset-background"
      role="group"
      aria-label={label}
    >
      <Button
        asChild
        size={size}
        variant={variant}
        className="pr-3 shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
        style={{ borderRadius: 0 }}
      >
        {primary}
      </Button>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <Button
            type="button"
            size={size}
            variant={variant}
            className="border-l border-current/25 px-2 shadow-none focus-visible:ring-0 focus-visible:ring-offset-0"
            style={{ borderRadius: 0 }}
            aria-label={`More actions for ${label}`}
          >
            <ChevronDown aria-hidden="true" />
          </Button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={4}
            className="z-50 min-w-44 rounded-lg border bg-popover p-1 text-popover-foreground shadow-xl"
          >
            {actions.map((action) => {
              const className = `flex cursor-default items-center gap-2 rounded-md px-3 py-2 text-sm outline-none focus:bg-muted data-[disabled]:pointer-events-none data-[disabled]:opacity-50 ${action.destructive ? "text-destructive focus:text-destructive" : ""}`;
              const content = <>{action.icon}{action.label}</>;
              return action.href ? (
                <DropdownMenu.Item key={action.id} disabled={action.disabled} className={className} asChild>
                  <a href={action.href} target={action.target} rel={action.target === "_blank" ? "noopener noreferrer" : undefined}>{content}</a>
                </DropdownMenu.Item>
              ) : (
                <DropdownMenu.Item key={action.id} disabled={action.disabled} className={className} onSelect={action.onSelect}>
                  {content}
                </DropdownMenu.Item>
              );
            })}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
  );
}
