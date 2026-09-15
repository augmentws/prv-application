"use client";

import { Eye, EyeOff, FileText, TableProperties } from "lucide-react";

import { CreateMetadataGroupDialog, type CreateMetadataGroupValues } from "@/components/forms/create-metadata-group-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { MetadataDefinitionRead, MetadataGroupRead } from "@/generated/models";

export function MetadataGroupsPanel({
  definitions,
  groups,
  onCreate,
  onVisibilityChange,
}: {
  definitions: MetadataDefinitionRead[];
  groups: MetadataGroupRead[];
  onCreate: (values: CreateMetadataGroupValues) => Promise<void>;
  onVisibilityChange: (group: MetadataGroupRead, surface: "TABLE" | "DOCUMENT", visible: boolean) => Promise<void>;
}) {
  const definitionNames = new Map(definitions.map((definition) => [definition.id, definition.display_name]));

  return (
    <div className="space-y-4">
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
        <div>
          <h2 className="text-base font-semibold">Metadata groups</h2>
          <p className="mt-1 text-sm text-muted-foreground">Choose which groups you see in tables and document views, or create a focused field set.</p>
        </div>
        <CreateMetadataGroupDialog definitions={definitions} onCreate={onCreate} />
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        {groups.map((group) => (
          <Card key={group.id}>
            <CardHeader className="gap-2">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div><CardTitle>{group.display_name}</CardTitle>{group.description ? <p className="mt-1 text-sm leading-5 text-muted-foreground">{group.description}</p> : null}</div>
                <Badge variant={group.scope === "PERSONAL" ? "accent" : "outline"}>{group.scope === "PERSONAL" ? "Only me" : group.scope.toLowerCase()}</Badge>
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap gap-1.5">
                {group.definition_ids.map((definitionId) => <Badge key={definitionId}>{definitionNames.get(definitionId) ?? "Unknown field"}</Badge>)}
              </div>
              <div className="border-t pt-4">
                <p className="mb-2 text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">Your visibility</p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant={group.table_visible ? "default" : "outline"}
                    aria-pressed={group.table_visible}
                    onClick={() => void onVisibilityChange(group, "TABLE", !group.table_visible)}
                  >
                    <TableProperties />Table {group.table_visible ? <Eye /> : <EyeOff />}
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant={group.document_visible ? "default" : "outline"}
                    aria-pressed={group.document_visible}
                    onClick={() => void onVisibilityChange(group, "DOCUMENT", !group.document_visible)}
                  >
                    <FileText />Document {group.document_visible ? <Eye /> : <EyeOff />}
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
