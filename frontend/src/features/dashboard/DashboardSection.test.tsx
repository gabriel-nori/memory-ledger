import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { DashboardSection } from "./DashboardSection";
import { renderApp } from "@/test/utils";
import { failWith, resetWrites, server, writes } from "@/test/server";
import { metaFixture } from "@/test/fixtures";

beforeEach(resetWrites);

/** The dashboard is a full CRUD client — these tests drive it as one. */
describe("Dashboard", () => {
  it("lists the rows the view returned, with its heading and count", async () => {
    renderApp(<DashboardSection />, { route: "/dashboard" });
    expect(await screen.findByText("Agenda — 2 items")).toBeInTheDocument();
    expect(screen.getByText(/Ship the new UI/)).toBeInTheDocument();
    expect(screen.getByText(/Write the docs/)).toBeInTheDocument();
  });

  it("shows the chips that say what a row is", async () => {
    renderApp(<DashboardSection />, { route: "/dashboard" });
    await screen.findByText(/Ship the new UI/);
    expect(screen.getByText("2026-09-20")).toBeInTheDocument();
    expect(screen.getByText("alex")).toBeInTheDocument();
    expect(screen.getByText("blocked ×1")).toBeInTheDocument();
    expect(screen.getByText("not set")).toBeInTheDocument();
  });

  it("switches view and asks the server for that one", async () => {
    let asked = "";
    server.use(
      http.get("/api/week", () => {
        asked = "week";
        return HttpResponse.json({ ok: true, view: "week", title: "This week", count: 0, results: [] });
      }),
    );
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await screen.findByText("Agenda — 2 items");
    await user.click(screen.getByRole("button", { name: "week" }));
    await waitFor(() => expect(asked).toBe("week"));
    expect(await screen.findByText("This week — 0 items")).toBeInTheDocument();
  });

  it("says why the list is empty rather than showing nothing", async () => {
    server.use(
      http.get("/api/agenda", () =>
        HttpResponse.json({ ok: true, view: "agenda", title: "Agenda", count: 0, results: [] }),
      ),
    );
    renderApp(<DashboardSection />, { route: "/dashboard" });
    expect(await screen.findByText("Nothing here. Clear head.")).toBeInTheDocument();
  });

  it("sends the filter chips as query params, and none it was not asked for", async () => {
    let search = "";
    server.use(
      http.get("/api/agenda", ({ request }) => {
        search = new URL(request.url).search;
        return HttpResponse.json({ ok: true, view: "agenda", title: "Agenda", count: 0, results: [] });
      }),
    );
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await screen.findByText("Agenda — 0 items");
    await user.click(screen.getByRole("button", { name: /Mine · needs action/ }));
    await waitFor(() => expect(search).toContain("mine=1"));
    // `since_days` belongs to the closed view; it must not ride along into agenda.
    expect(search).not.toContain("since_days");
  });

  it("reports a server error in place of the list", async () => {
    failWith("get", "/api/agenda", 500, "the brain is not readable");
    renderApp(<DashboardSection />, { route: "/dashboard" });
    expect(await screen.findByText("the brain is not readable")).toBeInTheDocument();
  });

  it("expands a card and shows what the node connects to", async () => {
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await user.click(await screen.findByRole("button", { name: /Ship the new UI/ }));
    expect(await screen.findByText(/The whole viz, as a real app/)).toBeInTheDocument();
    expect(screen.getByText(/Related \(2\)/)).toBeInTheDocument();
    expect(screen.getByText(/← relates-to: Alex/)).toBeInTheDocument();
  });

  it("completes a task through the archive endpoint, never a delete", async () => {
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await user.click(await screen.findByRole("button", { name: /Ship the new UI/ }));
    await user.click(await screen.findByRole("button", { name: "Complete" }));
    await waitFor(() => expect(writes).toContainEqual({ path: "/api/complete", body: { id: "task_ship-ui_a1" } }));
  });

  it("offers only the statuses the engine enforces", async () => {
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await user.click(await screen.findByRole("button", { name: /Ship the new UI/ }));
    const select = await screen.findByRole("combobox", { name: "status" });
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(metaFixture.statuses);
  });

  it("never writes a status the person did not change", async () => {
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await user.click(await screen.findByRole("button", { name: /Ship the new UI/ }));
    const select = await screen.findByRole("combobox", { name: "status" });
    await user.selectOptions(select, "open"); // already open
    expect(writes.filter((w) => w.path === "/api/set-status")).toHaveLength(0);
    await user.selectOptions(select, "blocked");
    await waitFor(() =>
      expect(writes).toContainEqual({
        path: "/api/set-status",
        body: { id: "task_ship-ui_a1", status: "blocked", waiting_on: undefined },
      }),
    );
  });

  it("never writes an assignee that came back unchanged from a focus and blur", async () => {
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await user.click(await screen.findByRole("button", { name: /Ship the new UI/ }));
    const input = await screen.findByRole("textbox", { name: "assignee" });
    await user.click(input);
    await user.tab();
    expect(writes.filter((w) => w.path === "/api/set-assignee")).toHaveLength(0);
  });

  it("sets a due date as a timestamp the engine can store", async () => {
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    const chip = await screen.findByRole("button", { name: "not set" });
    await user.click(chip);
    const picker = await screen.findByLabelText(/due date for Write the docs/);
    await user.type(picker, "2026-10-01");
    await waitFor(() =>
      expect(writes).toContainEqual({
        path: "/api/set-due",
        body: { id: "task_write-docs_e5", due: "2026-10-01T12:00:00" },
      }),
    );
  });

  it("builds a relation by searching for the other end", async () => {
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await user.click(await screen.findByRole("button", { name: /Ship the new UI/ }));
    await user.type(await screen.findByRole("textbox", { name: /search for a node to link/ }), "Retro");
    await user.click(await screen.findByRole("button", { name: /Retro notes \(note\)/ }));
    await waitFor(() =>
      expect(writes).toContainEqual({
        path: "/api/link",
        body: { src: "task_ship-ui_a1", dst: "note_retro_b2", rel: "relates-to" },
      }),
    );
  });

  it("removes a relation using its real direction, not the one it is rendered from", async () => {
    const { user } = renderApp(<DashboardSection />, { route: "/dashboard" });
    await user.click(await screen.findByRole("button", { name: /Ship the new UI/ }));
    // The Alex edge renders as incoming, so unlink has to send Alex as src.
    await user.click(await screen.findByRole("button", { name: /remove relates-to link to Alex/ }));
    await waitFor(() =>
      expect(writes).toContainEqual({
        path: "/api/unlink",
        body: { src: "person_alex", dst: "task_ship-ui_a1", rel: "relates-to" },
      }),
    );
  });
});

describe("Dashboard, read-only server", () => {
  it("hides editing and says which kind of no it is", async () => {
    server.use(http.get("/api/meta", () => HttpResponse.json({ ...metaFixture, writable: false })));
    renderApp(<DashboardSection />, { route: "/dashboard" });
    expect(await screen.findByText(/read-only server \(--no-write\)/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /New task/ })).not.toBeInTheDocument();
  });

  it("distinguishes 'could not ask' from '--no-write'", async () => {
    failWith("get", "/api/meta", 500, "meta blew up");
    renderApp(<DashboardSection />, { route: "/dashboard" });
    expect(await screen.findByText(/Writes unavailable/)).toBeInTheDocument();
  });
});
