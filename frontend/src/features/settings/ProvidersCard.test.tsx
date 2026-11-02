import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { ProvidersCard } from "./ProvidersCard";
import { renderApp } from "@/test/utils";
import { resetWrites, writes } from "@/test/server";

beforeEach(resetWrites);

const sent = (path: string) =>
  writes.filter((w) => w.path === path).map((w) => w.body as Record<string, unknown>);

describe("AI providers", () => {
  it("shows each provider's state and never a saved key", async () => {
    const { container } = renderApp(<ProvidersCard writable />, { route: "/settings" });
    const list = within(await screen.findByRole("list", { name: "providers" }));
    expect(list.getByText("installed 2.1.0")).toBeInTheDocument();
    expect(list.getByText("not installed")).toBeInTheDocument();
    expect(list.getByText("API key saved")).toBeInTheDocument();
    expect(container.querySelector("input[type=password]")).toHaveValue("");
  });

  it("installs only after the person confirms", async () => {
    const { user } = renderApp(<ProvidersCard writable />, { route: "/settings" });
    const list = within(await screen.findByRole("list", { name: "providers" }));
    await user.click(list.getAllByRole("button", { name: "Install" })[1]);
    expect(sent("/api/agent-provider-install")).toHaveLength(0);
    await user.click(await screen.findByRole("button", { name: "Run it" }));
    await waitFor(() => expect(sent("/api/agent-provider-install")).toEqual([{ kind: "gemini", confirm: true }]));
  });

  it("saves a typed key and clears the field", async () => {
    const { user } = renderApp(<ProvidersCard writable />, { route: "/settings" });
    const list = within(await screen.findByRole("list", { name: "providers" }));
    const field = list.getAllByLabelText(/API key/)[0];
    await user.type(field, "sk-secret");
    await user.click(list.getAllByRole("button", { name: "Save key" })[0]);
    await waitFor(() =>
      expect(sent("/api/agent-provider-key")).toEqual([{ kind: "claude", key: "sk-secret", remove: false }]));
    await waitFor(() => expect(field).toHaveValue(""));
  });

  it("saves the delegation block", async () => {
    const { user } = renderApp(<ProvidersCard writable />, { route: "/settings" });
    await user.click(await screen.findByLabelText("Allow delegation"));
    await user.click(screen.getByRole("button", { name: "Save delegation" }));
    await waitFor(() => {
      const [body] = sent("/api/agent-config");
      expect((body.delegation as { enabled: boolean }).enabled).toBe(true);
    });
  });
});
