import { beforeEach, describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { renderApp } from "@/test/utils";
import { server, writes, resetWrites } from "@/test/server";
import { UpdateBanner } from "./UpdateBanner";

const newer = (extra: Record<string, unknown> = {}) =>
  http.get("/api/update", () =>
    HttpResponse.json({ ok: true, current: "0.10.0", latest: "v0.11.0", available: true,
      installable: true, why_not: "", notes: "", url: "", error: "", ...extra }));

describe("the update banner", () => {
  beforeEach(() => {
    localStorage.clear();
    resetWrites();
  });

  it("says nothing when the install is current", async () => {
    renderApp(<UpdateBanner />, { withMeta: false });
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("offers the new version and installs it on Update", async () => {
    server.use(newer());
    const { user } = renderApp(<UpdateBanner />, { withMeta: false });
    expect(await screen.findByText(/Version v0.11.0 is out/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Update" }));
    expect(await screen.findByText(/Installing v0.11.0/)).toBeInTheDocument();
    expect(writes.map((w) => w.path)).toContain("/api/update/apply");
  });

  it("hides that version for good on Later", async () => {
    server.use(newer());
    const { user } = renderApp(<UpdateBanner />, { withMeta: false });
    await user.click(await screen.findByRole("button", { name: "Later" }));
    expect(screen.queryByText(/is out/)).toBeNull();
    expect(localStorage.getItem("ml:update-dismissed")).toBe("v0.11.0");
  });

  it("tells a git checkout to pull instead of offering Update", async () => {
    server.use(newer({ installable: false, why_not: "this is a git checkout: update it with git pull" }));
    renderApp(<UpdateBanner />, { withMeta: false });
    expect(await screen.findByText(/git pull/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Update" })).toBeNull();
  });
});
