import { describe, expect, it } from "vitest";

import type { MetadataDefinitionRead, MetadataGroupRead } from "@/generated/models";
import { organizeMetadataFilterDefinitions } from "@/lib/metadata-filter-groups";

function definition(id: string, displayName: string): MetadataDefinitionRead {
  return { id, key: id, display_name: displayName } as MetadataDefinitionRead;
}

function group(
  id: string,
  displayName: string,
  sortOrder: number,
  definitionIds: string[],
  status: MetadataGroupRead["status"] = "ACTIVE",
): MetadataGroupRead {
  return {
    id,
    display_name: displayName,
    sort_order: sortOrder,
    definition_ids: definitionIds,
    status,
  } as MetadataGroupRead;
}

describe("organizeMetadataFilterDefinitions", () => {
  it("uses metadata group and field ordering, assigns overlaps to the first group, and preserves ungrouped filters", () => {
    const definitions = [
      definition("built-in:coded_by", "Coded by"),
      definition("custodian", "Custodian"),
      definition("privilege", "Privilege"),
      definition("responsive", "Responsiveness"),
      definition("orphan-z", "Zeta"),
      definition("orphan-a", "Alpha"),
    ];
    const groups = [
      group("review", "Review coding", 20, ["responsive", "privilege"]),
      group("source", "Source", 10, ["custodian", "responsive"]),
      group("archived", "Archived", 0, ["orphan-z"], "ARCHIVED"),
    ];

    expect(organizeMetadataFilterDefinitions(definitions, groups, {
      documentDefinitionIds: new Set(["built-in:coded_by"]),
    }).map((item) => ({
      name: item.displayName,
      fields: item.definitions.map((field) => field.display_name),
    }))).toEqual([
      { name: "Document fields", fields: ["Coded by"] },
      { name: "Source", fields: ["Custodian", "Responsiveness"] },
      { name: "Review coding", fields: ["Privilege"] },
      { name: "Matter metadata", fields: ["Alpha", "Zeta"] },
    ]);
  });
});
