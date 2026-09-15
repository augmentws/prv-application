interface MimePart {
  mediaType: string;
  disposition: string;
  transferEncoding: string;
  charset: string;
  boundary: string | null;
  body: string;
}

export interface EmailBody {
  text: string;
  source: "plain" | "html" | "raw";
}

function bytesToLatin1(bytes: Uint8Array) {
  let result = "";
  const chunkSize = 8192;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    result += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return result;
}

function parameter(value: string, name: string) {
  const expression = new RegExp(`(?:^|;)\\s*${name}\\s*=\\s*(?:"([^"]*)"|([^;\\s]*))`, "i");
  const match = value.match(expression);
  return match?.[1] ?? match?.[2] ?? null;
}

function splitMessage(value: string) {
  const separator = /\r?\n\r?\n/.exec(value);
  if (!separator || separator.index === undefined) return { headers: "", body: value };
  return {
    headers: value.slice(0, separator.index),
    body: value.slice(separator.index + separator[0].length),
  };
}

function parseHeaders(block: string) {
  const headers = new Map<string, string>();
  const unfolded = block.replace(/\r?\n[\t ]+/g, " ");
  for (const line of unfolded.split(/\r?\n/)) {
    const colon = line.indexOf(":");
    if (colon < 1) continue;
    const name = line.slice(0, colon).trim().toLowerCase();
    const value = line.slice(colon + 1).trim();
    headers.set(name, headers.has(name) ? `${headers.get(name)}, ${value}` : value);
  }
  return headers;
}

function parsePart(value: string): MimePart {
  const { headers: headerBlock, body } = splitMessage(value);
  const headers = parseHeaders(headerBlock);
  const contentType = headers.get("content-type") ?? "text/plain; charset=utf-8";
  return {
    mediaType: contentType.split(";", 1)[0].trim().toLowerCase(),
    disposition: (headers.get("content-disposition") ?? "").toLowerCase(),
    transferEncoding: (headers.get("content-transfer-encoding") ?? "7bit").toLowerCase(),
    charset: parameter(contentType, "charset") ?? "utf-8",
    boundary: parameter(contentType, "boundary"),
    body,
  };
}

function latin1ToBytes(value: string) {
  return Uint8Array.from(value, (character) => character.charCodeAt(0) & 0xff);
}

function quotedPrintableBytes(value: string) {
  const bytes: number[] = [];
  for (let index = 0; index < value.length; index += 1) {
    if (value[index] === "=" && value[index + 1] === "\r" && value[index + 2] === "\n") {
      index += 2;
      continue;
    }
    if (value[index] === "=" && value[index + 1] === "\n") {
      index += 1;
      continue;
    }
    const encoded = value.slice(index + 1, index + 3);
    if (value[index] === "=" && /^[0-9a-f]{2}$/i.test(encoded)) {
      bytes.push(Number.parseInt(encoded, 16));
      index += 2;
      continue;
    }
    bytes.push(value.charCodeAt(index) & 0xff);
  }
  return Uint8Array.from(bytes);
}

function decodeBytes(bytes: Uint8Array, charset: string) {
  try {
    return new TextDecoder(charset).decode(bytes);
  } catch {
    return new TextDecoder("utf-8").decode(bytes);
  }
}

function decodePart(part: MimePart) {
  let bytes = latin1ToBytes(part.body);
  if (part.transferEncoding === "base64") {
    try {
      const decoded = atob(part.body.replace(/\s/g, ""));
      bytes = latin1ToBytes(decoded);
    } catch {
      // Leave malformed content readable in its original form.
    }
  } else if (part.transferEncoding === "quoted-printable") {
    bytes = quotedPrintableBytes(part.body);
  }
  return decodeBytes(bytes, part.charset);
}

function htmlToText(value: string) {
  if (typeof DOMParser === "undefined") return value.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  const document = new DOMParser().parseFromString(value, "text/html");
  document.querySelectorAll("br").forEach((element) => element.replaceWith("\n"));
  document.querySelectorAll("p, div, li, tr").forEach((element) => element.append("\n"));
  return (document.body.textContent ?? "").replace(/\n{3,}/g, "\n\n").trim();
}

function findBodies(part: MimePart): { plain: string[]; html: string[] } {
  if (part.mediaType.startsWith("multipart/") && part.boundary) {
    const marker = `--${part.boundary}`;
    const bodies = { plain: [] as string[], html: [] as string[] };
    for (const fragment of part.body.split(marker).slice(1)) {
      const trimmed = fragment.replace(/^\r?\n/, "");
      if (!trimmed || trimmed.startsWith("--")) continue;
      const nested = findBodies(parsePart(trimmed.replace(/\r?\n$/, "")));
      bodies.plain.push(...nested.plain);
      bodies.html.push(...nested.html);
    }
    return bodies;
  }
  if (part.disposition.startsWith("attachment")) return { plain: [], html: [] };
  if (part.mediaType === "text/plain") return { plain: [decodePart(part).trim()], html: [] };
  if (part.mediaType === "text/html") return { plain: [], html: [htmlToText(decodePart(part))] };
  return { plain: [], html: [] };
}

export function emailBody(bytes: Uint8Array): EmailBody {
  const source = bytesToLatin1(bytes);
  const root = parsePart(source);
  const bodies = findBodies(root);
  const plain = bodies.plain.find(Boolean);
  if (plain) return { text: plain, source: "plain" };
  const html = bodies.html.find(Boolean);
  if (html) return { text: html, source: "html" };
  return { text: decodePart(root).trim(), source: "raw" };
}

export function textDocument(bytes: Uint8Array, mediaType: string) {
  const charset = parameter(mediaType, "charset") ?? "utf-8";
  return decodeBytes(bytes, charset);
}
