import type { AgentEvent } from "@/api/types";

export type Segment = { said: string; thought?: boolean; events: AgentEvent[] };

/**
 * The reply's events in order: what the agent said on its own (a `text` event, a subagent's too) or thought (a top-level `thinking` event, labelled) as a message, each run of tool calls between those as one
 * folded log. The last thing said, when it is the reply itself, already shown below,
 * is dropped once the reply has landed.
 */
const MARKERS = /[ \t`]*\[\[(?:next:[\s\S]*?|needs-answer)\]\][ \t`]*/g;
const bare = (text: string) => text.replace(MARKERS, "").trim();
/** The server clips a logged text at 2000 chars and appends this note. */
const CLIPPED = /… \(\d+ more chars\)$/;
/** A marker the clip cut before its closing brackets. */
const OPEN_MARKER = /[ \t`]*\[\[(?:next:[\s\S]*|needs-answer)$/;

/** Whether a logged text is the reply itself, whole or clipped. */
function isReply(output: string, reply: string): boolean {
  const want = reply.trim();
  if (bare(output) === want) return true;
  if (!CLIPPED.test(output)) return false;
  const head = bare(output.replace(CLIPPED, "")).replace(OPEN_MARKER, "").trimEnd();
  return head.length > 0 && want.startsWith(head);
}

export function segments(events: AgentEvent[], landed: boolean, reply: string): Segment[] {
  const said = (e: AgentEvent) => (e.kind === "text" || (e.kind === "thinking" && !e.parent)) && !!(e.output ?? "").trim();
  const isText = (e: AgentEvent) => e.kind === "text";
  let last = -1;
  if (landed) events.forEach((e, i) => (last = said(e) && isText(e) && !e.parent && isReply(e.output ?? "", reply) ? i : last));
  const out: Segment[] = [];
  events.forEach((e, i) => {
    if (i === last) return;
    if (said(e)) out.push({ said: e.output ?? "", thought: !isText(e), events: [] });
    else if (out.length && !out[out.length - 1]!.said) out[out.length - 1]!.events.push(e);
    else out.push({ said: "", events: [e] });
  });
  return out;
}
