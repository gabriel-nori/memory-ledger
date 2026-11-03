import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { InboundHttpCard } from "./InboundHttpCard";
import { renderApp } from "@/test/utils";
import { writes } from "@/test/server";

const sent = (path: string) => writes.filter((w) => w.path === path).map((w) => w.body);

describe("InboundHttpCard", () => {
  it("lists the ways in with port, IPs, throttle and token, and says they listen here", async () => {
    renderApp(<InboundHttpCard writable />, { route: "/settings" });
    expect(await screen.findByText("site-chat")).toBeInTheDocument();
    expect(screen.getByText("listening here")).toBeInTheDocument();
    expect(screen.getByText("8801")).toBeInTheDocument();
    expect(screen.getByText("10.0.0.0/24")).toBeInTheDocument();
    expect(screen.getByText("300/min")).toBeInTheDocument();
    expect(screen.getByText("broken.yaml: not valid YAML")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "broken on" })).toBeDisabled();
  });

  it("switches a way in on, and the listeners on", async () => {
    const { user } = renderApp(<InboundHttpCard writable />, { route: "/settings" });
    await user.click(await screen.findByRole("checkbox", { name: "site-chat on" }));
    await waitFor(() => expect(sent("/api/inbound-http-trigger-enable")).toEqual([{ id: "site-chat", enabled: true }]));
    await user.click(screen.getByRole("checkbox", { name: /Open the listeners/ }));
    await waitFor(() => expect(sent("/api/trigger-config")).toEqual([{ inbound_http: { enabled: false } }]));
  });

  it("asks before it regenerates, shows the new token once, and deletes only on confirm", async () => {
    const { user } = renderApp(<InboundHttpCard writable />, { route: "/settings" });
    await user.click(await screen.findByRole("button", { name: "Regenerate token" }));
    expect(sent("/api/inbound-http-action")).toEqual([]);
    await user.click(screen.getByRole("button", { name: "confirm regenerate" }));
    expect(await screen.findByText(/New token for site-chat \(shown once\): inb_new-token/)).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    await user.click(screen.getByRole("button", { name: "cancel" }));
    expect(sent("/api/inbound-http-action")).toEqual([{ id: "site-chat", action: "regenerate" }]);
  });

  it("opens the fields, validates what they build, and shows the calls", async () => {
    const { user } = renderApp(<InboundHttpCard writable />, { route: "/settings" });
    await user.click((await screen.findAllByRole("button", { name: "Edit" }))[0]);
    const port = await screen.findByLabelText(/^Port/);
    expect((port as HTMLInputElement).value).toBe("8801");
    expect(screen.getByRole("table", { name: "calls" })).toBeInTheDocument();
    await user.clear(port);
    await user.type(port, "80");
    await user.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByText("port must be a whole number from 8801 to 8899")).toBeInTheDocument();
    const bodies = sent("/api/inbound-http-trigger") as { check_only: boolean; text: string }[];
    expect(bodies.every((b) => b.check_only)).toBe(true);
    expect(bodies[0].text).toContain("port: 80\n");
  });

  it("creates a new way in from the fields, shows its token once and folds the editor", async () => {
    const { user } = renderApp(<InboundHttpCard writable />, { route: "/settings" });
    await user.type(await screen.findByLabelText("New way in, id"), "second");
    await user.click(screen.getByRole("button", { name: "New way in" }));
    expect(((await screen.findByLabelText("Name")) as HTMLInputElement).value).toBe("second");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Token for second \(shown once\): inb_shown-once/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    expect((sent("/api/inbound-http-trigger").at(-1) as { text: string }).text).toContain("id: second");
  });
});
