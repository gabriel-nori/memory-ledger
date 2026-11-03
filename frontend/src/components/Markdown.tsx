import { useMemo } from "react";
import { parseBlocks } from "@/lib/markdown";
import { RichText } from "./RichText";
import { DocCard, ImageGallery } from "./DocCard";
import "./Markdown.css";

/**
 * A message with its Markdown: headings, lists, quotes, tables and rules on top
 * of what `RichText` reads inline, and `[name](doc:path)` as a document card
 * (an image shows as a thumbnail) that opens the file in an overlay. Built from React elements, never HTML.
 */
export function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => parseBlocks(text), [text]);
  return (
    <div className="ml-md">
      {blocks.map((b, i) => {
        switch (b.t) {
          case "p":
            return <p key={i}><RichText text={b.v} /></p>;
          case "h": {
            const H = `h${Math.min(b.level + 2, 6)}` as "h3" | "h4" | "h5" | "h6";
            return <H key={i}><RichText text={b.v} /></H>;
          }
          case "list": {
            const L = b.ordered ? "ol" : "ul";
            return (
              <L key={i}>
                {b.items.map((it, j) => <li key={j}><RichText text={it} /></li>)}
              </L>
            );
          }
          case "quote":
            return <blockquote key={i}><RichText text={b.v} /></blockquote>;
          case "hr":
            return <hr key={i} />;
          case "table":
            return (
              <div key={i} className="ml-md__table">
                <table>
                  <thead><tr>{b.head.map((c, j) => <th key={j}><RichText text={c} /></th>)}</tr></thead>
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j}>{r.map((c, k) => <td key={k}><RichText text={c} /></td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          case "doc":
            return <DocCard key={i} name={b.name} path={b.path} area={b.area} />;
          case "gallery":
            return <ImageGallery key={i} images={b.images} />;
        }
      })}
    </div>
  );
}
