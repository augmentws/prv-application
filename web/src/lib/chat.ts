export function chatTitleFromFirstMessage(firstMessage: string): string {
  return Array.from(firstMessage.trim().replace(/\s+/g, " ")).slice(0, 50).join("");
}
