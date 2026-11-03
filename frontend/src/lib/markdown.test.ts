import { describe, expect, it } from "vitest";
import { parseBlocks, safeDocPath, splitAttachments } from "./markdown";

describe("parseBlocks", () => {
  it("reads headings, lists, quotes and rules", () => {
    const b = parseBlocks("# Title\n\n- a\n- b\n\n1. x\n2. y\n\n> quoted\n\n---");
    expect(b.map((x) => x.t)).toEqual(["h", "list", "list", "quote", "hr"]);
    expect(b[1]).toEqual({ t: "list", ordered: false, items: ["a", "b"] });
    expect(b[2]).toMatchObject({ ordered: true, items: ["x", "y"] });
  });

  it("joins loose lines into one paragraph", () => {
    expect(parseBlocks("one\ntwo")).toEqual([{ t: "p", v: "one\ntwo" }]);
  });

  it("reads a table", () => {
    const b = parseBlocks("| a | b |\n|---|---|\n| 1 | 2 |");
    expect(b).toEqual([{ t: "table", head: ["a", "b"], rows: [["1", "2"]] }]);
  });

  it("leaves a code fence alone, whatever is inside", () => {
    const b = parseBlocks("```\n# not a heading\n- not a list\n```");
    expect(b).toHaveLength(1);
    expect(b[0].t).toBe("p");
  });

  it("turns a doc link on its own line into a card", () => {
    expect(parseBlocks("[Report](doc:documents/r.html)")).toEqual([
      { t: "doc", name: "Report", path: "documents/r.html", area: "exports" },
    ]);
  });

  it("refuses a doc path that leaves exports", () => {
    expect(safeDocPath("../x.md")).toBe(false);
    expect(safeDocPath("/etc/passwd")).toBe(false);
    expect(parseBlocks("[x](doc:../x.md)")[0].t).toBe("p");
  });
});

describe("attachments", () => {
  it("reads an attach link as a card in the attachments area", () => {
    expect(parseBlocks("[shot.png](attach:20261007-101010-ab12-shot.png)")).toEqual([
      { t: "doc", name: "shot.png", path: "20261007-101010-ab12-shot.png", area: "attachments" },
    ]);
  });

  it("splits a message's words from the files it ends with", () => {
    const text = "look at these\n\n[a.png](attach:x-a.png)\n[b.zip](attach:x-b.zip)";
    expect(splitAttachments(text)).toEqual({
      body: "look at these",
      files: [{ name: "a.png", path: "x-a.png" }, { name: "b.zip", path: "x-b.zip" }],
    });
  });

  it("refuses a path that leaves the folder", () => {
    expect(splitAttachments("[x](attach:../secret)").files).toEqual([]);
  });
});

describe("image galleries", () => {
  it("keeps one image as a doc and groups two or more into a gallery", () => {
    expect(parseBlocks("[a.webp](img:a.webp)")).toEqual([{ t: "doc", name: "a.webp", path: "a.webp", area: "images" }]);
    expect(parseBlocks("[a.webp](img:a.webp)\n[b.webp](img:b.webp)\n[c.webp](img:c.webp)")).toEqual([
      { t: "gallery", images: [{ name: "a.webp", path: "a.webp" }, { name: "b.webp", path: "b.webp" }, { name: "c.webp", path: "c.webp" }] },
    ]);
  });
});
