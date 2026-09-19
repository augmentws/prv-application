const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export function parseDateOnly(value: string) {
  if (!DATE_ONLY_PATTERN.test(value)) return null;
  const date = new Date(`${value}T00:00:00.000Z`);
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) return null;
  return date;
}

export function utcDayStart(value: string) {
  return parseDateOnly(value) ? `${value}T00:00:00.000Z` : null;
}

export function utcDayEnd(value: string) {
  return parseDateOnly(value) ? `${value}T23:59:59.999Z` : null;
}

export function formatDateOnly(value: string) {
  const date = parseDateOnly(value);
  if (!date) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}
