// A parser for a server-sent-event body read in chunks, as fetch delivers it.
// EventSource cannot POST, so the playground reads /v1/generate's stream itself.

export interface SseMessage {
  event: string;
  data: string;
  id: string | null;
}

/**
 * Returns a function to feed with text as it arrives. Each call returns the messages that
 * the text completed; a message split across chunks is held until its blank line comes.
 */
export function createSseParser(): (chunk: string) => SseMessage[] {
  let pending = "";
  return (chunk) => {
    pending += chunk.replace(/\r\n/g, "\n");
    const messages: SseMessage[] = [];
    let end = pending.indexOf("\n\n");
    while (end !== -1) {
      const block = pending.slice(0, end);
      pending = pending.slice(end + 2);
      const message = parseBlock(block);
      if (message !== null) messages.push(message);
      end = pending.indexOf("\n\n");
    }
    return messages;
  };
}

function parseBlock(block: string): SseMessage | null {
  let event = "message";
  let id: string | null = null;
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // a comment, such as a keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id") id = value;
  }
  return data.length === 0 ? null : { event, data: data.join("\n"), id };
}
