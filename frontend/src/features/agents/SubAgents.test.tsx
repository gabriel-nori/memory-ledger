import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SpawnForm, SubAgentBlocks } from "./SubAgents";
import type { AgentMessage } from "@/api/types";

const sub = (over: Partial<AgentMessage>): AgentMessage => ({
  id: "s1", conv_id: "c", role: "subagent", text: "", at: "2026-10-31T10:00:00Z", status: "done", reply_to: "r1",
  connector: "claude", model: "m", full_tools: false, events: [], context: [], error: "", session_id: "",
  cost_usd: null, cost_estimated: false, tokens_in: 0, tokens_out: 0, started_at: "", finished_at: "", node_id: "",
  next_action: "", parent_msg_id: "r1", label: "Explore", prompt: "find the thing", ...over,
});

describe("SubAgentBlocks", () => {
  it("shows what each does and its state, folded, with the nested one in view", () => {
    render(
      <SubAgentBlocks
        parentId="r1"
        writable
        onCancel={() => {}}
        subs={[sub({}), sub({ id: "s2", parent_msg_id: "s1", label: "Deep", status: "running", prompt: "dig" })]}
      />,
    );
    expect(screen.getByText("Explore")).toBeInTheDocument();
    expect(screen.getByText("Deep")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.queryByText("the answer")).toBeNull();
  });

  it("expands to the response", async () => {
    render(<SubAgentBlocks parentId="r1" writable onCancel={() => {}} subs={[sub({ text: "the answer" })]} />);
    await userEvent.click(screen.getByRole("button", { name: /Explore/ }));
    expect(screen.getByText("the answer")).toBeInTheDocument();
  });

  it("renders nothing for a message with no sub-agents", () => {
    const { container } = render(<SubAgentBlocks parentId="zzz" writable onCancel={() => {}} subs={[sub({})]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("SpawnForm", () => {
  it("starts a sub-agent with the typed task", async () => {
    const onSpawn = vi.fn().mockResolvedValue(undefined);
    render(<SpawnForm writable onSpawn={onSpawn} />);
    await userEvent.click(screen.getByRole("button", { name: "Sub-agent" }));
    await userEvent.type(screen.getByLabelText("Sub-agent task"), "check A");
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(onSpawn).toHaveBeenCalledWith("check A");
  });
});

describe("SubAgentBlocks transcript", () => {
  it("shows what the sub-agent said as a block and folds its tool calls", async () => {
    const events = [
      { kind: "text" as const, name: "text", output: "looking at the config" },
      { kind: "tool" as const, name: "Read", tool: "Read", input: "a.py", output: "x" },
    ];
    render(<SubAgentBlocks parentId="r1" writable onCancel={() => {}} subs={[sub({ events })]} />);
    await userEvent.click(screen.getByRole("button", { name: /Explore/ }));
    expect(screen.getByText("looking at the config")).toBeInTheDocument();
    expect(screen.getByText("Read").closest("details")).not.toHaveAttribute("open");
  });
});
