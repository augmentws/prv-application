"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Ellipsis } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";

export interface ActionMenuItem {
  id: string;
  label: string;
  icon?: ReactNode;
  disabled?: boolean;
  destructive?: boolean;
  onSelect: () => void;
}

export function ActionMenuButton({ label, actions }: { label: string; actions: ActionMenuItem[] }) {
  return <DropdownMenu.Root>
    <DropdownMenu.Trigger asChild>
      <Button type="button" size="sm" variant="outline" className="px-2" aria-label={`Actions for ${label}`}>
        <Ellipsis aria-hidden="true" />
      </Button>
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content align="end" sideOffset={4} className="z-50 min-w-44 rounded-lg border bg-popover p-1 text-popover-foreground shadow-xl">
        {actions.map((action) => <DropdownMenu.Item
          key={action.id}
          disabled={action.disabled}
          className={`flex cursor-default items-center gap-2 rounded-md px-3 py-2 text-sm outline-none focus:bg-muted data-[disabled]:pointer-events-none data-[disabled]:opacity-50 ${action.destructive ? "text-destructive focus:text-destructive" : ""}`}
          onSelect={action.onSelect}
        >
          {action.icon}{action.label}
        </DropdownMenu.Item>)}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>;
}
