import { describe, expect, it } from "vitest";
import { segments } from "./segments";
import type { AgentEvent } from "@/api/types";

describe("segments", () => {
  it("drops the last said text when it is the reply minus its [[next:]] marker", () => {
    const text = { kind: "text", output: "Done.\n\n[[next: ok]]" } as AgentEvent;
    expect(segments([text], true, "Done.")).toEqual([]);
  });

  it("drops the last said text when the server clipped it", () => {
    const reply = "A".repeat(2400) + " fim.";
    const text = { kind: "text", output: "A".repeat(2000) + "… (405 more chars)" } as AgentEvent;
    expect(segments([text], true, reply)).toEqual([]);
  });

  it("drops a clipped text whose [[next:]] marker was cut open", () => {
    const text = { kind: "text", output: "Pronto.\n\n[[next: Sobe o MySQL e roda… (28 more chars)" } as AgentEvent;
    expect(segments([text], true, "Pronto.")).toEqual([]);
  });

  it("keeps a clipped text that is not the reply", () => {
    const text = { kind: "text", output: "B".repeat(2000) + "… (10 more chars)" } as AgentEvent;
    expect(segments([text], true, "outra coisa")).toHaveLength(1);
  });
});
