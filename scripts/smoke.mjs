import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const apiDirectory = join(root, "apps/api");
const webDirectory = join(root, "apps/web");
const webRequire = createRequire(join(webDirectory, "package.json"));

async function availablePort() {
  const server = createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address === "object");
  const port = address.port;
  server.close();
  await once(server, "close");
  return port;
}

function start(command, args, cwd, environment) {
  const child = spawn(command, args, {
    cwd,
    env: { ...process.env, ...environment },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  let spawnError;
  const collect = (chunk) => {
    output = (output + chunk.toString()).slice(-12_000);
  };
  child.stdout.on("data", collect);
  child.stderr.on("data", collect);
  child.on("error", (error) => {
    spawnError = error;
  });
  return {
    child,
    assertRunning() {
      if (spawnError) throw spawnError;
      assert.equal(child.exitCode, null, output);
      assert.equal(child.signalCode, null, output);
    },
    output: () => output,
  };
}

async function stop(service) {
  if (
    !service ||
    service.child.exitCode !== null ||
    service.child.signalCode !== null
  )
    return;
  const closed = once(service.child, "close");
  service.child.kill("SIGTERM");
  const force = setTimeout(() => service.child.kill("SIGKILL"), 5_000);
  try {
    await closed;
  } finally {
    clearTimeout(force);
  }
}

async function waitUntilHealthy(url, services) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    for (const service of services) service.assertRunning();
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1_000) });
      if (response.ok) return response;
    } catch {
      // The process may still be starting.
    }
    await delay(200);
  }
  throw new Error(
    `Service did not become healthy: ${url}\n${services.map((s) => s.output()).join("\n")}`,
  );
}

let api;
let web;

try {
  const apiPort = await availablePort();
  let webPort = await availablePort();
  while (webPort === apiPort) webPort = await availablePort();
  const apiUrl = `http://127.0.0.1:${apiPort}`;
  const webUrl = `http://127.0.0.1:${webPort}`;
  const python = join(
    apiDirectory,
    process.platform === "win32"
      ? ".venv/Scripts/python.exe"
      : ".venv/bin/python",
  );
  api = start(
    python,
    [
      "-m",
      "uvicorn",
      "sitesense_api.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(apiPort),
    ],
    apiDirectory,
    {
      SITESENSE_ENVIRONMENT: "production",
    },
  );
  await waitUntilHealthy(`${apiUrl}/healthz`, [api]);
  web = start(
    process.execPath,
    [
      webRequire.resolve("next/dist/bin/next"),
      "start",
      "--hostname",
      "127.0.0.1",
      "--port",
      String(webPort),
    ],
    webDirectory,
    {
      NODE_ENV: "production",
      API_BASE_URL: apiUrl,
    },
  );
  const response = await waitUntilHealthy(`${webUrl}/api/health`, [api, web]);
  assert.deepEqual(await response.json(), {
    status: "ok",
    service: "sitesense-api",
  });
  const page = await fetch(webUrl, { signal: AbortSignal.timeout(5_000) });
  assert.equal(page.status, 200);
  assert.match(await page.text(), /SiteSense AI/);
  await stop(api);
  const unavailable = await fetch(`${webUrl}/api/health`, {
    signal: AbortSignal.timeout(7_000),
  });
  assert.equal(unavailable.status, 503);
  console.log(
    "Production smoke passed: homepage, typed API connection, and backend outage response.",
  );
} finally {
  await Promise.all([stop(web), stop(api)]);
}
