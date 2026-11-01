import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { AgentsSection } from "./AgentsSection";
import { renderApp } from "@/test/utils";
import { resetWrites, server, writes } from "@/test/server";
import { agentThreadFixture, voiceFixture } from "@/test/fixtures";
import { toText, formatCost } from "./export";

beforeEach(() => {
  resetWrites();
  // jsdom has no object URLs; the composer makes one per image for its thumbnail.
  URL.createObjectURL = () => "blob:thumb";
  URL.revokeObjectURL = () => {};
});

const sent = (path: string) => writes.filter((w) => w.path === path).map((w) => w.body);

describe("Agents", () => {
  it("lists active conversations with what each reply is doing, and folds hidden ones away", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents" });
    const list = await screen.findByRole("list", { name: "active conversations" });
    const rows = within(list).getAllByRole("button");
    expect(rows).toHaveLength(3);
    expect(within(rows[0]!).getByRole("img", { name: "the agent asked you something" })).toBeInTheDocument();
    expect(within(rows[1]!).getByRole("img", { name: "waiting for the agent" })).toBeInTheDocument();
    expect(within(rows[2]!).getByRole("img", { name: "answered" })).toBeInTheDocument();
    expect(screen.queryByText("Old idea")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Hidden \(1\)/ }));
    expect(within(screen.getByRole("list", { name: "hidden conversations" })).getByText("Old idea")).toBeInTheDocument();
  });

  it("puts yours on the right and the agent's on the left, its tool log folded shut", async () => {
    renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    const thread = await screen.findByRole("list", { name: "agent messages" });
    const mine = await within(thread).findByRole("listitem", { name: "your message" });
    const reply = within(thread).getByRole("listitem", { name: "agent reply" });
    expect(mine.className).toContain("--out");
    expect(reply.className).toContain("--in");
    const log = reply.querySelector("details.ml-agents__log") as HTMLDetailsElement;
    expect(log.open).toBe(false);
    expect(log.querySelector("summary")?.textContent).toMatch(/1 tool .*1 skill/);
    expect(within(reply).getByRole("img", { name: "the agent asked you something" })).toBeInTheDocument();
  });

  it("shows what the agent says while it works, without repeating its reply", async () => {
    renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    const reply = await screen.findByRole("listitem", { name: "agent reply" });
    expect(within(reply).getByText("Let me look for permits.")).toBeInTheDocument();
    expect(within(reply).getAllByText("The parking permit, by Oct 5. Should I add a task?")).toHaveLength(1);
    expect(reply.querySelectorAll("details.ml-agents__log")).toHaveLength(1);
  });

  it("keeps the unsent message per conversation, local first, and syncs it to the brain", async () => {
    localStorage.clear();
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    const box = await screen.findByRole("textbox", { name: "message to the agent" });
    await user.type(box, "half a thou");
    expect(JSON.parse(localStorage.getItem("ml.agent-draft.c1") ?? "{}").text).toBe("half a thou");
    await waitFor(() => expect(sent("/api/agent-conversation")).toEqual([expect.objectContaining({ id: "c1", draft: "half a thou" })]));
    expect(localStorage.getItem("ml.agent-draft.c2")).toBeNull();
  });

  it("takes the brain's draft when it is newer than the local one", async () => {
    localStorage.setItem("ml.agent-draft.c1", JSON.stringify({ text: "old", at: "2026-10-01T00:00:00Z" }));
    server.use(
      http.get("/api/agent-thread", () =>
        HttpResponse.json({
          ...agentThreadFixture,
          conversation: { ...agentThreadFixture.conversation, draft: "from the phone", draft_at: "2026-10-02T00:00:00Z" },
        }),
      ),
    );
    renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "message to the agent" })).toHaveValue("from the phone"));
  });

  it("cancels a reply only while it runs", async () => {
    const [mine, reply] = agentThreadFixture.messages;
    server.use(
      http.get("/api/agent-thread", () =>
        HttpResponse.json({ ...agentThreadFixture, messages: [mine, { ...reply!, status: "running", text: "" }] }),
      ),
    );
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    const running = await screen.findByRole("listitem", { name: "agent reply" });
    await user.click(await within(running).findByRole("button", { name: "Cancel the reply" }));
    await waitFor(() => expect(sent("/api/agent-cancel")).toEqual([{ id: "a1" }]));
  });

  it("offers no cancel on a reply that has landed", async () => {
    renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    const reply = await screen.findByRole("listitem", { name: "agent reply" });
    expect(within(reply).queryByRole("button", { name: "Cancel the reply" })).toBeNull();
  });

  it("shows the conversation's cost at the top right", async () => {
    renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    expect(await screen.findByLabelText("conversation cost")).toHaveTextContent("$0.01");
  });

  it("searches inside the chat", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    await screen.findByRole("listitem", { name: "agent reply" });
    await user.click(screen.getByRole("button", { name: "search in chat" }));
    await user.type(screen.getByLabelText("search in this chat"), "renew");
    expect(await screen.findByText("1 of 2 messages match")).toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "agent reply" })).not.toBeInTheDocument();
  });

  it("hides a conversation", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    await screen.findByRole("listitem", { name: "agent reply" });
    await user.click(screen.getByRole("button", { name: "Hide" }));
    await waitFor(() => expect(sent("/api/agent-conversation")).toEqual([{ id: "c1", hidden: true }]));
  });

  it("switches the model mid-conversation", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    await screen.findByRole("listitem", { name: "agent reply" });
    await user.click(await screen.findByRole("button", { name: "Options" }));
    await user.click(screen.getByRole("button", { name: "agent and model" }));
    await user.hover(screen.getByRole("menuitem", { name: /Ollama/ }));
    const models = screen.getByRole("menu", { name: "Ollama models" });
    await user.click(within(models).getByRole("menuitemradio", { name: "llama3:latest" }));
    await waitFor(() =>
      expect(sent("/api/agent-conversation")).toEqual([{ id: "c1", connector: "ollama", model: "llama3:latest" }]),
    );
    expect(screen.queryByRole("menu", { name: "agents" })).not.toBeInTheDocument();
  });

  it("opens an agent's models to the side, the current one ticked", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    await screen.findByRole("listitem", { name: "agent reply" });
    await user.click(await screen.findByRole("button", { name: "Options" }));
    await user.click(screen.getByRole("button", { name: "agent and model" }));
    const models = screen.getByRole("menu", { name: "Claude models" });
    expect(within(models).getByRole("menuitemradio", { name: "claude-sonnet-5-5" })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("menuitem", { name: /Ollama/ }));
    expect(screen.queryByRole("menu", { name: "Claude models" })).not.toBeInTheDocument();
    expect(screen.getByRole("menu", { name: "Ollama models" })).toBeInTheDocument();
  });

  it("gives the chat the whole window: no visible heading", async () => {
    renderApp(<AgentsSection />, { route: "/agents" });
    expect(await screen.findByRole("heading", { name: "Agents" })).toHaveClass("sr-only");
    expect(screen.queryByText(/Each turn is a fresh call/)).not.toBeInTheDocument();
  });

  it("ticks Read-only when the conversation is read-only, and unticking it turns tools on", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    await screen.findByRole("listitem", { name: "agent reply" });
    await user.click(screen.getByRole("button", { name: "Options" }));
    const box = screen.getByRole("checkbox", { name: "Read-only" });
    expect(box).toBeChecked();
    expect(box).toBeEnabled();
    await user.click(box);
    await waitFor(() => expect(sent("/api/agent-conversation")).toEqual([{ id: "c1", full_tools: true }]));
  });

  it("attaches files with the clip, the drop and the paste, and sends them with the message", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents" });
    const box = await screen.findByRole("textbox", { name: "message to the agent" });
    await user.click(await screen.findByRole("button", { name: "Options" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "agent and model" })).toHaveTextContent("claude-sonnet-5-5"));
    const png = new File(["x"], "shot.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("choose files"), png);
    const chips = await screen.findByRole("list", { name: "attached files" });
    expect(await within(chips).findByText("shot.png")).toBeInTheDocument();

    fireEvent.paste(box, { clipboardData: { files: [new File(["z"], "notes.zip", { type: "application/zip" })] } });
    expect(await within(chips).findByText("notes.zip")).toBeInTheDocument();

    const dropped = new File(["m"], "plan.md", { type: "text/markdown" });
    fireEvent.drop(box, { dataTransfer: { types: ["Files"], files: [dropped] } });
    expect(await within(chips).findByText("plan.md")).toBeInTheDocument();

    await user.click(within(chips).getByRole("button", { name: "Remove notes.zip" }));
    expect(within(chips).queryByText("notes.zip")).not.toBeInTheDocument();

    expect(sent("/api/agent-send")).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(sent("/api/agent-send")).toEqual([
        expect.objectContaining({
          text: "",
          attachments: ["20261007-101010-ab12-shot.png", "20261007-101010-ab12-plan.md"],
        }),
      ]),
    );
    expect(screen.queryByRole("list", { name: "attached files" })).not.toBeInTheDocument();
  });

  it("starts a conversation with the default agent, tools on", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents" });
    await screen.findByText("Start a conversation");
    await user.click(await screen.findByRole("button", { name: "Options" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "agent and model" })).toHaveTextContent("claude-sonnet-5-5"));
    await user.type(screen.getByLabelText("message to the agent"), "Hello{Enter}");
    await waitFor(() =>
      expect(sent("/api/agent-send")).toEqual([
        { text: "Hello", connector: "claude", model: "claude-sonnet-5-5", full_tools: true, context_turns: 1 },
      ]),
    );
  });
});

describe("agent export", () => {
  it("writes a conversation as plain text, tool calls listed", () => {
    const txt = toText(agentThreadFixture.conversation, agentThreadFixture.messages);
    expect(txt).toContain("# Parking permit");
    expect(txt).toContain("You:");
    expect(txt).toContain("· tool Grep");
    expect(txt).toContain("claude-sonnet-5-5 (claude) [question]:");
  });

  it("prices a local model as free and an estimate as approximate", () => {
    expect(formatCost(0, false, true)).toBe("$0 · local");
    expect(formatCost(0.0123, true)).toBe("≈ $0.01");
    expect(formatCost(0.0042)).toBe("$0.0042");
  });

  it("opens a fuzzy popup on a leading /, greys writers out while read-only, and inserts the pick", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents" });
    const box = await screen.findByRole("textbox", { name: "message to the agent" });
    await user.click(await screen.findByRole("button", { name: "Options" }));
    await screen.findByRole("button", { name: /Plugins/ });
    await user.click(screen.getByRole("checkbox", { name: "Read-only" }));
    await user.type(box, "/mlr");
    const list = await screen.findByRole("listbox", { name: "slash commands" });
    // letters in order: both memory-ledger commands match, the fuzzy tie sorts by name
    expect(within(list).getAllByRole("option").map((o) => o.textContent)).toEqual([
      expect.stringContaining("/memory-ledger:recall"),
      expect.stringContaining("/memory-ledger:send-slack"),
    ]);
    await user.clear(box);
    await user.type(box, "/slack");
    const writer = within(await screen.findByRole("listbox", { name: "slash commands" })).getByRole("option");
    expect(writer).toHaveAttribute("aria-disabled", "true");
    await user.clear(box);
    await user.type(box, "/rec{Enter}");
    expect(box).toHaveValue("/memory-ledger:recall ");
    expect(screen.queryByRole("listbox", { name: "slash commands" })).not.toBeInTheDocument();
  });

  it("does not open the popup unless the / starts the message", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents" });
    const box = await screen.findByRole("textbox", { name: "message to the agent" });
    await user.type(box, "see /rec");
    expect(screen.queryByRole("listbox", { name: "slash commands" })).not.toBeInTheDocument();
  });

  it("turns a plugin on from the Plugins menu", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents" });
    await user.click(await screen.findByRole("button", { name: "Options" }));
    await user.click(await screen.findByRole("button", { name: /Plugins/ }));
    await user.click(screen.getByRole("checkbox", { name: /chrome-devtools-mcp/ }));
    await waitFor(() =>
      expect(sent("/api/agent-plugin")).toEqual([{ key: "chrome-devtools-mcp@chrome-devtools-plugins", enabled: true }]),
    );
  });
});

describe("Agents voice", () => {
  it("puts dictation in the box, conversation mode where Send is, and speech in the options", async () => {
    // jsdom has no microphone; a browser that does is what the button needs.
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: vi.fn() }, configurable: true });
    vi.stubGlobal("AudioWorkletNode", class {});
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Dictate" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Conversation mode" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Speak" })).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "message to the agent" }), "hi");
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Conversation mode" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Options" }));
    expect(screen.getByRole("checkbox", { name: "Speak" })).not.toBeChecked();
  });

  it("saves speech and narration as this chat's own", async () => {
    const { user } = renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    await user.click(await screen.findByRole("button", { name: "Options" }));
    await user.click(screen.getByRole("checkbox", { name: "Speak" }));
    await waitFor(() => expect(sent("/api/agent-conversation")).toContainEqual(expect.objectContaining({ id: "c1", tts: "on" })));
    await user.selectOptions(screen.getByRole("combobox", { name: "narration" }), "verbose");
    await waitFor(() => expect(sent("/api/agent-conversation")).toContainEqual(expect.objectContaining({ id: "c1", verbosity: "verbose" })));
  });

  it("says why dictation is not available instead of failing on click", async () => {
    server.use(
      http.get("/api/voice", () =>
        HttpResponse.json({ ...voiceFixture, stt: { available: false, model: "medium", problem: "no Python with faster-whisper found" } }),
      ),
    );
    renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    const dictate = await screen.findByRole("button", { name: "Dictate" });
    await waitFor(() => expect(dictate).toBeDisabled());
    expect(dictate).toHaveAttribute("title", "no Python with faster-whisper found");
  });

  it("shows each block translated, with the original a click away, when conversion is on", async () => {
    server.use(
      http.get("/api/voice", () => HttpResponse.json({ ...voiceFixture, config: { ...voiceFixture.config, translate: true, translate_target: "it" } })),
    );
    renderApp(<AgentsSection />, { route: "/agents?c=c1" });
    const reply = await screen.findByRole("listitem", { name: "agent reply" });
    await waitFor(() => expect(within(reply).getByText("[it] Let me look for permits.")).toBeInTheDocument());
    expect(within(reply).getAllByText("original").length).toBeGreaterThan(0);
    expect(sent("/api/voice-translate")).toContainEqual({ text: "Let me look for permits.", target: "it" });
  });
});
