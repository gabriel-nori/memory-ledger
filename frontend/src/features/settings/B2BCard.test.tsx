import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { B2BCard } from "./B2BCard";
import { renderApp } from "@/test/utils";
import { failWith, resetWrites, writes } from "@/test/server";
import { b2bFixture } from "@/test/fixtures";

beforeEach(resetWrites);

const panel = async (name: string) =>
  within(await screen.findByRole("region", { name }));
const sent = (path: string) =>
  writes
    .filter((w) => w.path === path)
    .map((w) => w.body as Record<string, unknown>);

describe("B2B", () => {
  it("shows the identity, whether a key is set, and the brokers", async () => {
    renderApp(<B2BCard writable />, { route: "/settings" });
    const identity = await panel("Identity");
    expect(identity.getByText(b2bFixture.config.user_id)).toBeInTheDocument();
    expect(identity.getByText("set (file)")).toBeInTheDocument();
    const brokers = await panel("Brokers");
    expect(brokers.getByText("Optibus broker")).toBeInTheDocument();
    expect(brokers.getByText("Lab broker")).toBeInTheDocument();
    expect(brokers.getByText("logged in")).toBeInTheDocument();
  });

  it("never renders a private key, and sends one only when typed", async () => {
    const { user, container } = renderApp(<B2BCard writable />, {
      route: "/settings",
    });
    const identity = await panel("Identity");
    expect(container.innerHTML).not.toContain("private_key");
    const field = identity.getByLabelText("Import a private key");
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveValue("");
    await user.type(field, "sk-secret");
    await user.click(identity.getByRole("button", { name: "Import key" }));
    await waitFor(() =>
      expect(sent("/api/b2b-config")).toContainEqual({
        private_key: "sk-secret",
      }),
    );
    await waitFor(() => expect(field).toHaveValue(""));
  });

  it("asks before replacing an identity, and only then forces it", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const identity = await panel("Identity");
    await user.click(
      identity.getByRole("button", { name: "Replace identity" }),
    );
    expect(sent("/api/b2b-identity")).toHaveLength(0);
    expect(
      await screen.findByText(/can no longer be read/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Replace" }));
    await waitFor(() =>
      expect(sent("/api/b2b-identity")).toEqual([{ force: true }]),
    );
  });

  it("adds a broker with its url and name", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const brokers = await panel("Brokers");
    await user.type(
      brokers.getByLabelText("Broker URL"),
      "https://new.example.com",
    );
    await user.type(brokers.getByLabelText("Broker name"), "New one");
    await user.click(brokers.getByRole("button", { name: "Add broker" }));
    await waitFor(() =>
      expect(sent("/api/b2b-broker")).toEqual([
        { url: "https://new.example.com", name: "New one" },
      ]),
    );
  });

  it("switches a broker off, and checks its login inline", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const row = within(
      await screen.findByRole("listitem", { name: "Broker Optibus broker" }),
    );
    await user.click(row.getByRole("checkbox"));
    await waitFor(() =>
      expect(sent("/api/b2b-broker")).toContainEqual({
        id: "b1",
        enabled: false,
      }),
    );
    await user.click(row.getByRole("button", { name: "Check login" }));
    expect(await row.findByText(/Logged in, until/)).toBeInTheDocument();
    expect(sent("/api/b2b-broker-check")).toEqual([{ id: "b1" }]);
  });

  it("removes a broker only after confirming", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const row = within(
      await screen.findByRole("listitem", { name: "Broker Lab broker" }),
    );
    await user.click(row.getByRole("button", { name: "Remove" }));
    expect(sent("/api/b2b-broker")).toHaveLength(0);
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Remove",
      }),
    );
    await waitFor(() =>
      expect(sent("/api/b2b-broker")).toContainEqual({
        id: "b2",
        remove: true,
      }),
    );
  });

  it("adds a peer with the broker and person it was given", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const peers = await panel("Peers");
    const id = "C".repeat(43);
    await user.type(peers.getByLabelText("Peer user id"), id);
    await user.type(peers.getByLabelText("Peer name"), "Eli");
    await user.selectOptions(peers.getByLabelText("Peer broker"), "b2");
    await waitFor(() =>
      expect(peers.getByRole("option", { name: "Maria" })).toBeInTheDocument(),
    );
    await user.selectOptions(
      peers.getByLabelText("Linked person"),
      "person_maria",
    );
    await user.click(peers.getByRole("button", { name: "Add peer" }));
    await waitFor(() =>
      expect(sent("/api/b2b-peer")).toEqual([
        {
          peer_id: id,
          enabled: true,
          name: "Eli",
          broker_id: "b2",
          person_id: "person_maria",
        },
      ]),
    );
  });

  it("shows the linked person, and disconnects or reconnects a peer", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const dana = within(
      await screen.findByRole("listitem", { name: "Peer Dana" }),
    );
    expect(dana.getByText("Alex")).toBeInTheDocument();
    await user.click(dana.getByRole("button", { name: "Disconnect" }));
    await waitFor(() =>
      expect(sent("/api/b2b-peer")[0]).toMatchObject({
        peer_id: "A".repeat(43),
        enabled: false,
      }),
    );
    const old = within(
      screen.getByRole("listitem", { name: "Peer Old friend" }),
    );
    await user.click(old.getByRole("button", { name: "Reconnect" }));
    await waitFor(() =>
      expect(sent("/api/b2b-peer")[1]).toMatchObject({
        peer_id: "B".repeat(43),
        enabled: true,
      }),
    );
  });

  it("allows a sender by user id, and blocks or re-allows one", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const list = await panel("Allowed senders");
    const dana = within(list.getByRole("listitem", { name: "Sender Dana" }));
    expect(dana.getByText("peer")).toBeInTheDocument();
    await user.click(dana.getByRole("button", { name: "Block" }));
    await waitFor(() =>
      expect(sent("/api/b2b-allowed")[0]).toEqual({
        user_id: "A".repeat(43),
        enabled: false,
      }),
    );
    const spam = within(list.getByRole("listitem", { name: "Sender Spammer" }));
    expect(spam.getByText("blocked")).toBeInTheDocument();
    await user.click(spam.getByRole("button", { name: "Allow" }));
    await waitFor(() =>
      expect(sent("/api/b2b-allowed")[1]).toEqual({
        user_id: "D".repeat(43),
        enabled: true,
      }),
    );
    const id = "F".repeat(43);
    expect(list.getByRole("button", { name: "Allow sender" })).toBeDisabled();
    await user.type(list.getByLabelText("Sender user id"), id);
    await user.type(list.getByLabelText("Sender name"), "Gil");
    await user.click(list.getByRole("button", { name: "Allow sender" }));
    await waitFor(() =>
      expect(sent("/api/b2b-allowed")[2]).toEqual({
        user_id: id,
        enabled: true,
        name: "Gil",
      }),
    );
  });

  it("saves the poll interval and run command", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const rx = await panel("Receiving");
    const poll = rx.getByLabelText("Poll every (seconds)");
    await user.clear(poll);
    await user.type(poll, "120");
    await user.click(rx.getByRole("button", { name: "Save receiving" }));
    await waitFor(() =>
      expect(sent("/api/b2b-config")).toContainEqual({
        poll_seconds: 120,
        do: b2bFixture.config.do,
        action: "claude",
        agent: "",
        http: b2bFixture.config.http,
        script: b2bFixture.config.script,
      }),
    );
  });

  it("picks the agent a received message runs on", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const rx = await panel("Receiving");
    const pick = await rx.findByLabelText("Agent for a received B2B message");
    await user.selectOptions(pick, "claude");
    await user.click(rx.getByRole("button", { name: "Save receiving" }));
    await waitFor(() =>
      expect(sent("/api/b2b-config")).toContainEqual(expect.objectContaining({ agent: "claude" })),
    );
  });

  it("can run a script instead of Claude, like the other triggers", async () => {
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const rx = await panel("Receiving");
    await user.click(
      within(
        rx.getByRole("group", { name: "Action for a received B2B message" }),
      ).getByRole("button", { name: "Run a script" }),
    );
    expect(rx.queryByLabelText("Run command")).not.toBeInTheDocument();
    expect(rx.getByLabelText("Arguments")).toBeInTheDocument();
    await user.click(rx.getByRole("button", { name: "Save receiving" }));
    await waitFor(() =>
      expect(sent("/api/b2b-config")).toContainEqual(
        expect.objectContaining({ action: "script" }),
      ),
    );
  });

  it("shows a refused write with the server's reason", async () => {
    failWith(
      "post",
      "/api/b2b-broker",
      400,
      "url must start with http:// or https://",
    );
    const { user } = renderApp(<B2BCard writable />, { route: "/settings" });
    const brokers = await panel("Brokers");
    await user.type(brokers.getByLabelText("Broker URL"), "ftp://x");
    await user.click(brokers.getByRole("button", { name: "Add broker" }));
    expect(
      await brokers.findByText(/must start with http/),
    ).toBeInTheDocument();
  });
});
