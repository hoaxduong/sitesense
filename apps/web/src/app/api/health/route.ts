import { createApiClient } from "@sitesense/api-client";

const responseHeaders = { "Cache-Control": "no-store" };

function unavailable(message = "Backend connection is unavailable.") {
  return Response.json(
    { status: "unavailable", message },
    { status: 503, headers: responseHeaders },
  );
}

export async function GET() {
  const configuredBaseUrl = process.env.API_BASE_URL?.trim();
  const baseUrl =
    configuredBaseUrl ||
    (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : null);

  if (!baseUrl) {
    return unavailable("Backend connection is not configured.");
  }

  try {
    const client = createApiClient(baseUrl);
    const { data, response } = await client.GET("/api/v1/health", {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });

    if (
      !response.ok ||
      data?.status !== "ok" ||
      data.service !== "sitesense-api"
    ) {
      return unavailable();
    }

    return Response.json(
      { status: "ok", service: "sitesense-api" },
      { headers: responseHeaders },
    );
  } catch {
    return unavailable();
  }
}
