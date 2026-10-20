import type { MetadataDefinitionRead, MetadataGroupRead } from "@/generated/models";

export interface MetadataFilterGroup {
  id: string;
  displayName: string;
  definitions: MetadataDefinitionRead[];
}

interface OrganizeMetadataFilterDefinitionsOptions {
  documentDefinitionIds?: ReadonlySet<string>;
}

/**
 * Organize filterable definitions using the same first-active-group rule as
 * the search export field catalog. Group and field ordering come from the
 * metadata group definition; fields outside a group remain available.
 */
export function organizeMetadataFilterDefinitions(
  definitions: MetadataDefinitionRead[],
  groups: MetadataGroupRead[],
  options: OrganizeMetadataFilterDefinitionsOptions = {},
): MetadataFilterGroup[] {
  const definitionById = new Map(definitions.map((definition) => [definition.id, definition]));
  const assignedDefinitionIds = new Set<string>();
  const result: MetadataFilterGroup[] = [];
  const documentDefinitions = [...(options.documentDefinitionIds ?? [])]
    .map((definitionId) => definitionById.get(definitionId))
    .filter((definition): definition is MetadataDefinitionRead => Boolean(definition));

  if (documentDefinitions.length) {
    documentDefinitions.forEach((definition) => assignedDefinitionIds.add(definition.id));
    result.push({ id: "document-fields", displayName: "Document fields", definitions: documentDefinitions });
  }

  const activeGroups = groups
    .filter((group) => group.status === "ACTIVE")
    .sort((left, right) => left.sort_order - right.sort_order
      || left.display_name.localeCompare(right.display_name)
      || left.id.localeCompare(right.id));

  for (const group of activeGroups) {
    const groupDefinitions = group.definition_ids
      .map((definitionId) => definitionById.get(definitionId))
      .filter((definition): definition is MetadataDefinitionRead => Boolean(
        definition && !assignedDefinitionIds.has(definition.id),
      ));
    if (!groupDefinitions.length) continue;
    groupDefinitions.forEach((definition) => assignedDefinitionIds.add(definition.id));
    result.push({ id: group.id, displayName: group.display_name, definitions: groupDefinitions });
  }

  const ungroupedDefinitions = definitions
    .filter((definition) => !assignedDefinitionIds.has(definition.id))
    .sort((left, right) => left.display_name.localeCompare(right.display_name) || left.key.localeCompare(right.key));
  if (ungroupedDefinitions.length) {
    result.push({ id: "matter-metadata", displayName: "Matter metadata", definitions: ungroupedDefinitions });
  }

  return result;
}
