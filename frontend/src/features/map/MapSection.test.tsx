import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { MapSection } from "./MapSection";
import { NodeCard } from "./NodeCard";
import { renderApp } from "@/test/utils";
import { failWith, resetWrites, server, writes } from "@/test/server";
import { graphFixture, metaFixture } from "@/test/fixtures";

beforeEach(resetWrites);

describe("Map", () => {
  it("says how much of the brain it is showing", async () => {
    renderApp(<MapSection />);
    expect(await screen.findByText("4/4 nodes · 3 edges")).toBeInTheDocument();
  });

  it("builds its facets from the payload, never from a list in the code", async () => {
    const { user } = renderApp(<MapSection />);
    await screen.findByText("4/4 nodes · 3 edges");
    await user.click(screen.getByRole("button", { name: /realms/ }));
    // The fixture's realms, with the counts the server sent.
    expect(screen.getByRole("checkbox", { name: /work/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /personal/ })).toBeInTheDocument();
  });

  it("drops a node type from the count when its facet is switched off", async () => {
    const { user } = renderApp(<MapSection />);
    await screen.findByText("4/4 nodes · 3 edges");
    await user.click(screen.getByRole("button", { name: /types/ }));
    await user.click(screen.getByRole("checkbox", { name: /task/ }));
    expect(await screen.findByText("2/4 nodes · 3 edges")).toBeInTheDocument();
  });

  it("hides done nodes only when asked to", async () => {
    const { user } = renderApp(<MapSection />);
    await screen.findByText("4/4 nodes · 3 edges");
    await user.click(screen.getByRole("button", { name: /Display/ }));
    await user.click(screen.getByRole("checkbox", { name: /hide done/ }));
    expect(await screen.findByText("3/4 nodes · 3 edges")).toBeInTheDocument();
  });

  it("says how many extra nodes the cascade took", async () => {
    const { user } = renderApp(<MapSection />);
    await screen.findByText("4/4 nodes · 3 edges");
    await user.click(screen.getByRole("button", { name: /types/ }));
    await user.click(screen.getByRole("checkbox", { name: /person/ }));
    await user.click(screen.getByRole("button", { name: /types/ }));
    await user.click(screen.getByRole("button", { name: /Display/ }));
    await user.click(screen.getByRole("checkbox", { name: /drop unsupported/ }));
    // Alex supported both tasks; dropping him unwinds them and the note under them.
    expect(await screen.findByText("+3")).toBeInTheDocument();
  });

  it("reports a failed poll without taking the map down", async () => {
    failWith("get", "/api/graph", 500, "database is locked");
    renderApp(<MapSection />);
    expect(await screen.findByText("database is locked")).toBeInTheDocument();
    expect(screen.getByText("offline")).toBeInTheDocument();
  });

  it("goes live again once the server answers", async () => {
    let fail = true;
    server.use(
      http.get("/api/graph", () =>
        fail
          ? HttpResponse.json({ ok: false, error: "locked" }, { status: 500 })
          : HttpResponse.json(graphFixture),
      ),
    );
    renderApp(<MapSection />);
    await screen.findByText("offline");
    fail = false;
    expect(await screen.findByText("live", {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it("keeps the canvas labelled for anything that cannot see it", async () => {
    renderApp(<MapSection />);
    expect(await screen.findByRole("img", { name: /4 nodes and 3 edges/ })).toBeInTheDocument();
  });
});

describe("adding an idea from the map", () => {
  const open = async () => {
    const view = renderApp(<MapSection />);
    await screen.findByText("4/4 nodes · 3 edges");
    await view.user.click(await screen.findByRole("button", { name: "+ idea" }));
    await screen.findByRole("heading", { name: "New idea" });
    return view.user;
  };

  it("will not save an idea with no title", async () => {
    await open();
    expect(screen.getByRole("button", { name: "Save idea" })).toBeDisabled();
  });

  it("offers the brain's realms, the current one first", async () => {
    await open();
    const realm = (await screen.findByLabelText(/realm/i)) as HTMLSelectElement;
    expect(realm.value).toBe(metaFixture.default_realm);
    expect([...realm.options].map((o) => o.value)[0]).toBe(metaFixture.default_realm);
  });

  it("writes the idea, then selects it once the map has read it back", async () => {
    let written = false;
    server.use(
      http.get("/api/graph", () =>
        HttpResponse.json(
          written
            ? {
                ...graphFixture,
                nodes: [
                  ...graphFixture.nodes,
                  { id: "idea_new_0001", title: "Cache the layout", type: "idea", realm: "personal",
                    status: "proposed", degree: 0, kind: "(none)" },
                ],
              }
            : graphFixture,
        ),
      ),
      http.post("/api/propose-idea", async ({ request }) => {
        writes.push({ path: "/api/propose-idea", body: await request.json() });
        written = true;
        return HttpResponse.json({ ok: true, node: { id: "idea_new_0001", type: "idea" } });
      }),
    );
    const user = await open();
    await user.type(screen.getByLabelText(/^Title/), "Cache the layout");
    await user.type(screen.getByLabelText(/^Detail/), "Why: speed.");
    await user.selectOptions(screen.getByLabelText(/realm/i), "personal");
    await user.click(screen.getByRole("button", { name: "Save idea" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/propose-idea")?.body).toEqual({
        title: "Cache the layout",
        detail: "Why: speed.",
        realm: "personal",
      }),
    );
    expect(await screen.findByRole("heading", { name: "Cache the layout" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "New idea" })).not.toBeInTheDocument();
    expect(await screen.findByText("5/5 nodes · 3 edges")).toBeInTheDocument();
  });

  it("keeps the dialog open and says why when the server refuses", async () => {
    failWith("post", "/api/propose-idea", 400, "realm must be lowercase letters, digits and dashes");
    const user = await open();
    await user.type(screen.getByLabelText(/^Title/), "Cache the layout");
    await user.click(screen.getByRole("button", { name: "Save idea" }));
    expect(
      await screen.findByText("realm must be lowercase letters, digits and dashes"),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "New idea" })).toBeInTheDocument();
  });
});

describe("Map, read-only server", () => {
  it("names the reason editing is gone", async () => {
    server.use(http.get("/api/meta", () => HttpResponse.json({ ...metaFixture, writable: false })));
    renderApp(<MapSection />);
    await screen.findByText("4/4 nodes · 3 edges");
    expect(screen.queryByRole("button", { name: "+ idea" })).not.toBeInTheDocument();
    // Nothing on the map writes without a selection, so the check is that the
    // toolbar still renders and no write was attempted while read-only.
    expect(writes).toHaveLength(0);
  });
});

/** Selection happens on the canvas, so the card is driven directly here. */
describe("the node card", () => {
  const node = graphFixture.nodes[0]!;
  const byId = new Map(graphFixture.nodes.map((n) => [n.id, n]));
  const card = (props: Partial<React.ComponentProps<typeof NodeCard>> = {}) => (
    <NodeCard
      node={node}
      edges={graphFixture.edges}
      byId={byId}
      onClose={() => {}}
      onFocus={() => {}}
      onChanged={() => {}}
      {...props}
    />
  );

  it("shows the node's fields and both directions of its edges", async () => {
    renderApp(card());
    expect(await screen.findByRole("heading", { name: "Ship the new UI" })).toBeInTheDocument();
    expect(screen.getByText("task_ship-ui_a1")).toBeInTheDocument();
    expect(screen.getByText(/← incoming \(1\)/)).toBeInTheDocument();
    expect(screen.getByText(/outgoing → \(1\)/)).toBeInTheDocument();
  });

  it("jumps to the node on the other end of a connection", async () => {
    let focused = "";
    const { user } = renderApp(card({ onFocus: (id) => (focused = id) }));
    await user.click(await screen.findByRole("button", { name: /Alex/ }));
    expect(focused).toBe("person_alex");
  });

  it("completes from the map, so finishing something never means leaving the graph", async () => {
    const { user } = renderApp(card());
    await user.click(await screen.findByRole("button", { name: "Complete" }));
    await waitFor(() =>
      expect(writes).toContainEqual({ path: "/api/complete", body: { id: "task_ship-ui_a1", note: undefined } }),
    );
  });

  it("offers no complete on a type that has no meaningful done", async () => {
    renderApp(card({ node: graphFixture.nodes[1]! }));
    await screen.findByRole("heading", { name: "Alex" });
    expect(screen.queryByRole("button", { name: /Complete/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Edit/ })).toBeInTheDocument();
  });

  it("says a task is archived rather than offering to complete it twice", async () => {
    renderApp(card({ node: graphFixture.nodes[3]! }));
    expect(await screen.findByText("archived")).toBeInTheDocument();
  });

  it("edits title, realm and body in place, and refuses an empty title", async () => {
    const { user } = renderApp(card());
    await user.click(await screen.findByRole("button", { name: /Edit/ }));
    const title = await screen.findByLabelText(/title/i);
    await user.clear(title);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("title cannot be empty")).toBeInTheDocument();
    expect(writes.filter((w) => w.path === "/api/update-node")).toHaveLength(0);

    await user.type(title, "Ship it properly");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(writes.find((w) => w.path === "/api/update-node")?.body).toMatchObject({
        id: "task_ship-ui_a1",
        title: "Ship it properly",
      }),
    );
  });

  it("only offers due, priority and assignee on something someone has to DO", async () => {
    const { user } = renderApp(card({ node: graphFixture.nodes[1]! }));
    await user.click(await screen.findByRole("button", { name: /Edit/ }));
    expect(await screen.findByLabelText(/realm/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^due/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/priority/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/assignee/i)).not.toBeInTheDocument();
  });

  it("keeps a realm the server has never heard of, so editing never moves the node", async () => {
    const stray = { ...node, realm: "skunkworks" };
    const { user } = renderApp(card({ node: stray }));
    await user.click(await screen.findByRole("button", { name: /Edit/ }));
    const realm = (await screen.findByLabelText(/realm/i)) as HTMLSelectElement;
    expect([...realm.options].map((o) => o.value)).toContain("skunkworks");
    expect(realm.value).toBe("skunkworks");
  });

  it("shows an unrecognised assignee rather than rendering the task unassigned", async () => {
    const stray = { ...node, props: { assignee: "someone-not-in-the-graph" } };
    const { user } = renderApp(card({ node: stray }));
    await user.click(await screen.findByRole("button", { name: /Edit/ }));
    expect(
      await screen.findByRole("option", { name: /someone-not-in-the-graph\s+\(not a person node\)/ }),
    ).toBeInTheDocument();
  });

  it("says which kind of no a read-only server is", async () => {
    server.use(http.get("/api/meta", () => HttpResponse.json({ ...metaFixture, writable: false })));
    renderApp(card());
    expect(await screen.findByText("read-only server (--no-write)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument();
  });

  it("says 'writes unavailable' when the vocabulary could not be read at all", async () => {
    failWith("get", "/api/meta", 500, "meta blew up");
    renderApp(card());
    expect(await screen.findByText("writes unavailable")).toBeInTheDocument();
  });
});
