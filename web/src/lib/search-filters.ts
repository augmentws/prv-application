export const NO_VALUE_FILTER_TOKEN = "\u0000pvr-no-value";
export const HAS_VALUE_FILTER_TOKEN = "\u0000pvr-has-value";

export function toggleFacetFilterToken(selected: string[], value: string): string[] {
  if ([HAS_VALUE_FILTER_TOKEN, NO_VALUE_FILTER_TOKEN].includes(value)) {
    return selected.includes(value) ? [] : [value];
  }
  const withoutExistenceFilters = selected.filter(
    (item) => ![HAS_VALUE_FILTER_TOKEN, NO_VALUE_FILTER_TOKEN].includes(item),
  );
  return withoutExistenceFilters.includes(value)
    ? withoutExistenceFilters.filter((item) => item !== value)
    : [...withoutExistenceFilters, value];
}
