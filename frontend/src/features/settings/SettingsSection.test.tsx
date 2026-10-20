import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { SettingsSection } from "./SettingsSection";
import { renderApp } from "@/test/utils";
import { failWith, resetWrites, server, writes } from "@/test/server";
import { doctorFixture, settingsFixture, statusFixture } from "@/test/fixtures";

beforeEach(resetWrites);

describe("Settings", () => {
  it("shows what is stored and what is actually in effect", async () => {
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByLabelText(/Timezone/)).toHaveValue("America/Sao_Paulo");
    expect(screen.getByText(/In effect: America\/Sao_Paulo \(from settings\)/)).toBeInTheDocument();
  });

  it("writes preferences server-side, so the CLI and the hooks agree with the page", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    const tz = await screen.findByLabelText(/Timezone/);
    await user.clear(tz);
    await user.type(tz, "Europe/Lisbon");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/settings")?.body).toMatchObject({
        tz: "Europe/Lisbon",
      }),
    );
  });

  it("sends an empty value rather than skipping it, because empty REMOVES a key", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    const tz = await screen.findByLabelText(/Timezone/);
    await user.clear(tz);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      const body = writes.find((w) => w.path === "/api/settings")?.body as Record<string, unknown>;
      expect(body).toHaveProperty("tz", "");
    });
  });

  it("warns when the shell has taken a key over, instead of pretending the save took effect", async () => {
    server.use(
      http.get("/api/settings", () =>
        HttpResponse.json({
          ...settingsFixture,
          env: { tz: "UTC" },
          effective: { ...settingsFixture.effective, tz: "UTC", tz_source: "env" },
        }),
      ),
    );
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByText(/The shell has taken over tz/)).toBeInTheDocument();
  });

  it("reports a refused save with the server's reason", async () => {
    failWith("post", "/api/settings", 400, "unknown timezone: Mars/Olympus");
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await screen.findByLabelText(/Timezone/);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("unknown timezone: Mars/Olympus")).toBeInTheDocument();
  });

  it("disables saving on a read-only server", async () => {
    server.use(http.get("/api/settings", () => HttpResponse.json({ ...settingsFixture, writable: false })));
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByRole("button", { name: "Save" })).toBeDisabled();
    // Every card that writes says so, so this is a count, not a lookup.
    expect(screen.getAllByText("read-only server").length).toBeGreaterThan(0);
  });

  it("writes the owner, because who 'me' is used to be a constant in the source", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    const field = await screen.findByLabelText("Your name");
    await user.clear(field);
    await user.type(field, "Ana Silva");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/settings")?.body).toMatchObject({
        owner: "Ana Silva",
      }),
    );
  });

  it("names the brain, and says what the name is for", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    const field = await screen.findByLabelText("The brain's name");
    expect(screen.getAllByText(/\("Jarvis will handle this"\)/).length).toBeGreaterThan(0);
    await user.type(field, "Friday");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/settings")?.body).toMatchObject({ brain_name: "Friday" }),
    );
  });

  it("offers the realms the graph actually has, rather than a typed-in string", async () => {
    renderApp(<SettingsSection />, { route: "/settings" });
    const select = (await screen.findByLabelText("Default realm")) as HTMLSelectElement;
    const values = [...select.options].map((o) => o.value);
    expect(values).toContain("work");
    // "" is the "fall back to the default" option, not a realm.
    expect(values).toContain("");
  });

  it("renames a realm only after confirming, and says when it merges into one that exists", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await user.selectOptions(await screen.findByLabelText("Rename realm"), "work");
    await user.type(screen.getByLabelText("New name"), "personal");
    expect(screen.getByText(/personal already exists/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rename" }));
    expect(writes.find((w) => w.path === "/api/rename-realm")).toBeUndefined();
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Rename" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/rename-realm")?.body).toEqual({ old: "work", new: "personal" }),
    );
    expect(await screen.findByText(/merged into the existing realm/)).toBeInTheDocument();
  });

  it("refuses a realm name the engine would refuse, before sending it", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await user.type(await screen.findByLabelText("New name"), "Side Projects");
    expect(screen.getByRole("button", { name: "Rename" })).toBeDisabled();
    expect(screen.getByText(/Lowercase letters, digits and dashes only/)).toBeInTheDocument();
  });

  it("adds a realm by making it where new nodes land", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await user.type(await screen.findByLabelText(/Add a realm/), "side-projects");
    await user.click(screen.getByRole("button", { name: "Add and make default" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/settings")?.body).toEqual({ realm: "side-projects" }),
    );
  });

  it("says when an env var has taken a switch over, instead of offering a dead box", async () => {
    server.use(
      http.get("/api/settings", () =>
        HttpResponse.json({
          ...settingsFixture,
          effective: {
            ...settingsFixture.effective,
            autosync: false,
            autosync_source: "env" as const,
          },
        }),
      ),
    );
    renderApp(<SettingsSection />, { route: "/settings" });
    const box = await screen.findByRole("checkbox", { name: /Push every write to the mirror/ });
    expect(box).toBeDisabled();
    expect(box).not.toBeChecked();
    expect(screen.getByText(/MEMORY_LEDGER_NO_AUTOSYNC is set/)).toBeInTheDocument();
  });
});

describe("Settings — capture", () => {
  it("says whether the brain is recording, and can stop it", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByText("recording")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Turn capture off" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/capture")?.body).toMatchObject({ enabled: false }),
    );
  });

  it("refuses to offer a switch the environment has already won", async () => {
    server.use(
      http.get("/api/settings", () =>
        HttpResponse.json({
          ...settingsFixture,
          capture: { ...settingsFixture.capture, enabled: false, env_off: true, source: "env" as const },
        }),
      ),
    );
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByRole("button", { name: "Turn capture on" })).toBeDisabled();
    expect(screen.getByText(/outranks the marker file/)).toBeInTheDocument();
  });
});

describe("Settings — health", () => {
  it("runs the health check on demand, not on load", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    const button = await screen.findByRole("button", { name: "Run the health check" });
    expect(screen.queryByText("healthy")).not.toBeInTheDocument();
    await user.click(button);
    expect(await screen.findByText("healthy")).toBeInTheDocument();
    expect(screen.getByText("Nothing actionable is wrong.")).toBeInTheDocument();
  });

  it("lists the problems when there are any", async () => {
    server.use(
      http.get("/api/doctor", () =>
        HttpResponse.json({
          ...doctorFixture,
          healthy: false,
          problems: ["one or more hooks reference a missing script"],
        }),
      ),
    );
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await user.click(await screen.findByRole("button", { name: "Run the health check" }));
    expect(
      await screen.findByText("one or more hooks reference a missing script"),
    ).toBeInTheDocument();
  });
});

describe("Settings — where the data lives", () => {
  it("says where the brain is and how much of it there is", async () => {
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByText("/home/brain/graph.db")).toBeInTheDocument();
    expect(screen.getByText("sqlite (local mirror)")).toBeInTheDocument();
  });

  it("says the master is optional and never shows a password", async () => {
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByText("not configured")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Password/)).toHaveValue("");
    expect(screen.getByText("None stored yet.")).toBeInTheDocument();
  });

  it("keeps the stored password when the field is left blank", async () => {
    server.use(
      http.get("/api/status", () =>
        HttpResponse.json({
          ...statusFixture,
          neo4j: { ...statusFixture.neo4j, url: "neo4j+s://x", configured: true, up: true, password_set: true },
        }),
      ),
    );
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await screen.findByText("reachable");
    expect(screen.getByText("One is stored. Blank keeps it.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/neo4j-config")?.body).toMatchObject({ password: "" }),
    );
  });

  it("warns when the environment wins over the file", async () => {
    server.use(
      http.get("/api/status", () =>
        HttpResponse.json({
          ...statusFixture,
          neo4j: { ...statusFixture.neo4j, env: ["SECOND_BRAIN_NEO4J_URL"] },
        }),
      ),
    );
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByText(/SECOND_BRAIN_NEO4J_URL/)).toBeInTheDocument();
  });

  it("says how many writes are queued on the flush button", async () => {
    server.use(http.get("/api/status", () => HttpResponse.json({ ...statusFixture, pending_ops: 7 })));
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByRole("button", { name: "Flush queue (7)" })).toBeInTheDocument();
  });

  it("will not consolidate until it has shown what that would change", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await screen.findByText("/home/brain/graph.db");

    // Nothing has been previewed, so the button that writes is not available.
    expect(screen.getByRole("button", { name: "Consolidate" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("What this would change")).toBeInTheDocument();
    expect(screen.getByText("2026-09-19")).toBeInTheDocument();
    // A preview writes nothing at all.
    expect(writes.some((w) => w.path === "/api/prune")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Consolidate" }));
    // And still nothing, until the dialog is answered.
    expect(writes.some((w) => w.path === "/api/prune")).toBe(false);
    await user.click(await screen.findByRole("button", { name: "Consolidate 12 captures" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/prune")?.body).toMatchObject({ confirm: true }),
    );
  });

  it("changing the cutoff drops the plan, so a stale preview cannot be confirmed", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await screen.findByText("/home/brain/graph.db");
    // Found before the preview renders: a label lookup walks the whole page, and
    // once the plan is on screen that walk alone takes about a second.
    const cutoff = screen.getByLabelText("Keep the last N days loose");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText("What this would change");

    await user.clear(cutoff);
    await user.type(cutoff, "30");

    expect(screen.queryByText("What this would change")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Consolidate" })).toBeDisabled();
  });

  it("lists the snapshots consolidation leaves behind", async () => {
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByText("maintain-20260922T120000Z.db")).toBeInTheDocument();
    expect(screen.getByText(/expires 2026-09-27/)).toBeInTheDocument();
  });

  it("reports an unreachable master rather than staying quiet", async () => {
    failWith("post", "/api/neo4j-test", 400, "connection refused");
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    await screen.findByText("/home/brain/graph.db");
    await user.click(screen.getByRole("button", { name: "Test" }));
    expect(await screen.findByText(/Testing failed: connection refused/)).toBeInTheDocument();
  });
});

describe("Prompt recall", () => {
  it("is off by default and writes the switch through the settings route", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    const box = await screen.findByRole("checkbox", { name: /Recall related notes on each prompt/ });
    expect(box).not.toBeChecked();
    await user.click(box);
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/settings")?.body).toMatchObject({
        prompt_recall: true,
      }),
    );
  });
});

describe("Agents sync", () => {
  it("is off by default and writes the switch through the settings route", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    const box = await screen.findByRole("checkbox", { name: /Sync Agents chats across machines/ });
    expect(box).not.toBeChecked();
    await user.click(box);
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/settings")?.body).toMatchObject({
        agents_sync: true,
      }),
    );
  });
});

describe("Learned rules", () => {
  it("is off by default and writes the switch through the settings route", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    const box = await screen.findByRole("checkbox", { name: /Look for repeated corrections/ });
    expect(box).not.toBeChecked();
    await user.click(box);
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/settings")?.body).toMatchObject({
        learned_rules: true,
      }),
    );
  });

  it("lists what was found and accepts or rejects it, never applying silently", async () => {
    const { user } = renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByText("no, never use emojis in commit messages")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/accept-rule")?.body).toMatchObject({
        id: "proposal_0123456789",
      }),
    );
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/reject-rule")?.body).toMatchObject({
        id: "proposal_0123456789",
      }),
    );
  });

  it("cannot run the detector while it is off", async () => {
    renderApp(<SettingsSection />, { route: "/settings" });
    expect(await screen.findByRole("button", { name: "Look now" })).toBeDisabled();
  });
});
