export interface BatchDocumentReference {
  documentId: string;
  paragraphReference?: string;
}

export function parseParagraphNumbers(reference?: string | null): number[] {
  if (!reference?.trim()) return [];
  const values = new Set<number>();
  const expression = /(?:¶\s*)?(\d+)(?:\s*[-–—]\s*(?:¶\s*)?(\d+))?/g;
  for (const match of reference.matchAll(expression)) {
    const start = Number.parseInt(match[1], 10);
    const end = Number.parseInt(match[2] ?? match[1], 10);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) continue;
    // A citation should remain a navigation aid, not allocate an unbounded model-produced range.
    for (let value = start; value <= Math.min(end, start + 199); value += 1) values.add(value);
  }
  return [...values].sort((left, right) => left - right);
}

export function normalizeParagraphReference(reference?: string | null): string | undefined {
  const values = parseParagraphNumbers(reference);
  if (!values.length) return undefined;
  const ranges: string[] = [];
  let start = values[0];
  let end = start;
  for (const value of values.slice(1)) {
    if (value === end + 1) {
      end = value;
      continue;
    }
    ranges.push(start === end ? String(start) : `${start}-${end}`);
    start = value;
    end = value;
  }
  ranges.push(start === end ? String(start) : `${start}-${end}`);
  return ranges.join(",");
}
