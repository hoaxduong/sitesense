"use client";

import { useEffect, useRef, useState } from "react";

type ConnectionStatus = "checking" | "ok" | "unavailable";

async function checkHealth(signal: AbortSignal): Promise<ConnectionStatus> {
  try {
    const response = await fetch("/api/health", { cache: "no-store", signal });
    const data: unknown = await response.json();

    return response.ok &&
      typeof data === "object" &&
      data !== null &&
      "status" in data &&
      data.status === "ok"
      ? "ok"
      : "unavailable";
  } catch {
    return "unavailable";
  }
}

const statusLabels: Record<ConnectionStatus, string> = {
  checking: "Checking connection…",
  ok: "Connected",
  unavailable: "Unavailable",
};

export function BackendStatus() {
  const [status, setStatus] = useState<ConnectionStatus>("checking");
  const requestController = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    requestController.current = controller;
    void checkHealth(controller.signal).then((nextStatus) => {
      if (!controller.signal.aborted) setStatus(nextStatus);
    });
    return () => requestController.current?.abort();
  }, []);

  async function refresh() {
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    setStatus("checking");
    const nextStatus = await checkHealth(controller.signal);
    if (!controller.signal.aborted) setStatus(nextStatus);
  }

  return (
    <section className="status-card" aria-labelledby="backend-heading">
      <div className="card-heading">
        <h2 id="backend-heading">Backend connection</h2>
        <button
          className="refresh-button"
          onClick={() => void refresh()}
          disabled={status === "checking"}
          type="button"
        >
          Refresh
        </button>
      </div>
      <p className={`connection-status connection-${status}`} role="status">
        <span className="status-dot" aria-hidden="true" />
        {statusLabels[status]}
      </p>
      <p className="card-description">
        This checks the API service. A connected backend does not mean research
        data or prediction models are ready.
      </p>
    </section>
  );
}
