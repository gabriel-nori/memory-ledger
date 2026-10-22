import { useEffect, useState } from "react";
import { api, documentFileUrl } from "@/api/client";
import type { AgentDocument } from "@/api/types";
import { IMAGE_EXT } from "@/lib/markdown";
import type { DocArea } from "@/lib/markdown";
import { Button } from "./Button";
import { Modal } from "./Modal";
import { Spinner, StatusMessage } from "./Feedback";
import { Markdown } from "./Markdown";
import "./DocCard.css";
import { Icon } from "./Icon";

/**
 * A document an agent wrote: collapsed it is only its name and an Expand
 * button; expanded it overlays the interface, closing with the cross at the
 * top right, Escape, or a click outside it. The file is read only on expand.
 *
 * HTML and PDF show in a sandboxed frame (its script cannot reach this app's
 * API); Markdown is rendered; an image is shown as is; anything else is shown
 * as text, or, for a file we cannot show (a zip), offered as a download.
 *
 * An image is the exception to "collapsed": it shows as a thumbnail in the
 * message, and clicking it opens the viewer.
 */
function DownloadLink({ name, src }: { name: string; src: string }) {
  return (
    <a className="ml-btn ml-btn--default ml-btn--sm" href={src} download={name} aria-label={`Download ${name}`}>
      Download
    </a>
  );
}

export function DocCard({ name, path, area = "exports" }: { name: string; path: string; area?: DocArea }) {
  const [open, setOpen] = useState(false);
  const src = documentFileUrl(path, area);
  if (IMAGE_EXT.test(path))
    return (
      <>
        <button type="button" className="ml-doc__thumb" aria-label={`Expand ${name}`} onClick={() => setOpen(true)}>
          <img src={src} alt={name} loading="lazy" />
        </button>
        {open ? (
          <Modal title={name} subtitle={path} width="min(1100px, 96vw)" onClose={() => setOpen(false)}>
            <p><DownloadLink name={name} src={src} /></p>
            <img className="ml-doc__image" src={src} alt={name} />
          </Modal>
        ) : null}
      </>
    );
  return (
    <>
      <div className="ml-doc">
        <span className="ml-doc__icon" aria-hidden="true"><Icon name="file" /></span>
        <span className="ml-doc__name" title={path}>{name}</span>
        <Button size="sm" onClick={() => setOpen(true)} aria-label={`Expand ${name}`}>
          Expand
        </Button>
        <DownloadLink name={name} src={src} />
      </div>
      {open ? (
        <Modal title={name} subtitle={path} width="min(1100px, 96vw)" onClose={() => setOpen(false)}>
          <DocBody path={path} area={area} />
        </Modal>
      ) : null}
    </>
  );
}

function DocBody({ path, area }: { path: string; area: DocArea }) {
  const [doc, setDoc] = useState<AgentDocument | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const ctl = new AbortController();
    api.document(path, area, ctl.signal).then(setDoc, (e) => {
      if (!ctl.signal.aborted) setError(e instanceof Error ? e.message : String(e));
    });
    return () => ctl.abort();
  }, [path, area]);

  if (error) return <StatusMessage tone="error">{error}</StatusMessage>;
  if (!doc) return <Spinner label="reading the document" />;
  if (doc.kind === "html" || doc.kind === "pdf")
    return (
      <iframe
        className="ml-doc__frame"
        title={doc.name}
        src={documentFileUrl(path, area)}
        sandbox={doc.kind === "html" ? "allow-scripts" : undefined}
      />
    );
  if (doc.kind === "img") return <img className="ml-doc__image" src={documentFileUrl(path, area)} alt={doc.name} />;
  if (doc.kind === "file")
    return (
      <p>
        This kind of file has no preview.{" "}
        <a href={documentFileUrl(path, area)} download={doc.name}>Download {doc.name}</a>
      </p>
    );
  if (doc.kind === "md") return <Markdown text={doc.text ?? ""} />;
  return <pre className="ml-doc__text">{doc.text}</pre>;
}
