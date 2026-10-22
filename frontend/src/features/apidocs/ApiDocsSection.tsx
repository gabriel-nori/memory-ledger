import "./ApiDocsSection.css";
import { Icon } from "@/components";

/** Swagger UI, rendered by the server from the routes it is running. */
export const API_DOCS_URL = "./api/docs";

/**
 * The server's own API reference, inside the app.
 *
 * Swagger is rendered by serve.py, not by React, so it is framed rather than
 * reimplemented: the document it shows is generated from the running routes,
 * and a copy of it here would be a second description of the API to go stale.
 * Framing it keeps the drawer, the URL and everything else open in the app —
 * reading the API is something you do beside the brain, not instead of it. The
 * drawer's arrow still opens it in a tab of its own for when you want the room.
 */
export function ApiDocsSection() {
  return (
    <div className="ml-apidocs">
      <div className="ml-apidocs__bar">
        <h1 className="ml-apidocs__title">API docs</h1>
        <a className="ml-apidocs__out" href={API_DOCS_URL} target="_blank" rel="noreferrer">
          open in a new tab <span aria-hidden="true"><Icon name="external" /></span>
        </a>
      </div>
      <iframe className="ml-apidocs__frame" src={API_DOCS_URL} title="API docs" />
    </div>
  );
}
