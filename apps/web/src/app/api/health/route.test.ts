import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./route";

beforeEach(() => {
  vi.stubEnv("NODE_ENV", "production");
  vi.stubEnv("API_BASE_URL", "http://private-api.internal:8000");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("GET /api/health", () => {
  it("checks the generated API contract without caching", async () => {
    const fetchMock = vi.fn<(request: Request) => Promise<Response>>(async () =>
      Response.json({ status: "ok", service: "sitesense-api" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const timeout = vi.spyOn(AbortSignal, "timeout");

    const response = await GET();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      status: "ok",
      service: "sitesense-api",
    });
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    const request = fetchMock.mock.calls[0]?.[0];
    expect(request?.url).toBe("http://private-api.internal:8000/api/v1/health");
    expect(request?.cache).toBe("no-store");
    expect(timeout).toHaveBeenCalledWith(5_000);
  });

  it("returns a generic 503 for an upstream HTTP failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({ detail: "private error" }, { status: 500 }),
      ),
    );

    const response = await GET();

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      status: "unavailable",
      message: "Backend connection is unavailable.",
    });
  });

  it("rejects a successful HTTP response with an invalid health payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({ status: "ok", service: "another-api" }),
      ),
    );

    const response = await GET();

    expect(response.status).toBe(503);
    expect((await response.json()).status).toBe("unavailable");
  });

  it("does not expose internal network errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("Could not connect to private-api.internal:8000");
      }),
    );

    const response = await GET();

    expect(response.status).toBe(503);
    expect(await response.text()).not.toContain("private-api.internal");
  });

  it("returns 503 when the upstream request times out", async () => {
    const controller = new AbortController();
    const timeout = vi
      .spyOn(AbortSignal, "timeout")
      .mockReturnValue(controller.signal);
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (request: Request) =>
          new Promise<Response>((_resolve, reject) => {
            request.signal.addEventListener(
              "abort",
              () => reject(request.signal.reason),
              {
                once: true,
              },
            );
          }),
      ),
    );

    const pendingResponse = GET();
    controller.abort(new DOMException("Upstream timeout", "TimeoutError"));
    const response = await pendingResponse;

    expect(timeout).toHaveBeenCalledWith(5_000);
    expect(response.status).toBe(503);
    expect((await response.json()).status).toBe("unavailable");
  });

  it("requires explicit backend configuration in production", async () => {
    vi.stubEnv("API_BASE_URL", "");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET();

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      status: "unavailable",
      message: "Backend connection is not configured.",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses the local backend by default only during development", async () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("API_BASE_URL", "");
    const fetchMock = vi.fn<(request: Request) => Promise<Response>>(async () =>
      Response.json({ status: "ok", service: "sitesense-api" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const response = await GET();

    expect(response.status).toBe(200);
    expect(fetchMock.mock.calls[0]?.[0].url).toBe(
      "http://127.0.0.1:8000/api/v1/health",
    );
  });
});
