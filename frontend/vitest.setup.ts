import "@testing-library/jest-dom/vitest";
import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { server } from "./src/test/server";

// One mock server for the whole suite, asserting on unhandled requests: a
// component that quietly calls an endpoint nobody stubbed is a bug in the test
// OR in the component, and either way it should not pass silently.
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  // View preferences persist in localStorage on purpose — that is the feature.
  // Between TESTS it would mean one test's last filter silently becomes the
  // next one's starting state, so each begins from a fresh browser.
  window.localStorage.clear();
});
afterAll(() => server.close());

// jsdom has no canvas. The map's paint loop is exercised through the pure
// physics module instead; here it just needs to not throw.
HTMLCanvasElement.prototype.getContext = vi.fn(
  () =>
    ({
      clearRect: vi.fn(),
      beginPath: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      stroke: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      fillText: vi.fn(),
      setTransform: vi.fn(),
      globalAlpha: 1,
      lineWidth: 1,
      fillStyle: "",
      strokeStyle: "",
      font: "",
    }) as unknown as CanvasRenderingContext2D,
) as unknown as typeof HTMLCanvasElement.prototype.getContext;

// Neither does it have rAF timing worth simulating in a unit test.
if (!globalThis.requestAnimationFrame) {
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof requestAnimationFrame;
  globalThis.cancelAnimationFrame = ((id: number) => clearTimeout(id)) as typeof cancelAnimationFrame;
}

// jsdom ships its own AbortController, and Node's fetch (undici, which msw
// intercepts) rejects a signal that is not Node's own AbortSignal: "Expected
// signal to be an instance of AbortSignal". Newer Node versions are strict
// about it, so every request a component made with its abort signal failed in
// the suite. Take the signal off before the request reaches msw and honour it
// here instead: the call rejects with an AbortError once the signal fires. It
// wraps fetch AFTER server.listen patched it, so it is the outermost layer.
beforeAll(() => {
  const patched = globalThis.fetch.bind(globalThis);
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const signal = init?.signal;
    if (!signal) return patched(input, init);
    const { signal: _taken, ...rest } = init;
    const abortError = () => new DOMException("The operation was aborted.", "AbortError");
    if (signal.aborted) return Promise.reject(abortError());
    return new Promise<Response>((resolve, reject) => {
      signal.addEventListener("abort", () => reject(abortError()), { once: true });
      patched(input, rest).then(resolve, reject);
    });
  }) as typeof fetch;
});
