import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";

const root = process.cwd();
const children = [];
const defaultLeaApiBaseUrl = "http://127.0.0.1:8000";

function fail(message) {
  console.error(`\n[dev] ${message}`);
  process.exit(1);
}

function ensure(pathname, message) {
  if (!existsSync(path.join(root, pathname))) {
    fail(message);
  }
}

function readConfigStrings() {
  const configPath = path.join(root, "config", "lea.local.toml");
  const config = readFileSync(configPath, "utf8");
  const values = {};
  for (const key of [
    "lea_api_base_url",
    "lea_api_key",
    "google_api_key",
    "anthropic_api_key",
    "openai_api_key",
    "openai_base_url",
  ]) {
    const match = config.match(new RegExp(`^\\s*${key}\\s*=\\s*"([^"]*)"`, "m"));
    if (match) {
      values[key] = match[1];
    }
  }
  // Additional provider keys are stored as uppercase ENV_VAR_NAME root keys
  // (e.g. MISTRAL_API_KEY) and injected into the Lea API env verbatim.
  const extraApiKeys = {};
  const envKeyPattern = /^\s*([A-Z][A-Z0-9_]*_API_KEY)\s*=\s*"([^"]*)"/gm;
  let envMatch;
  while ((envMatch = envKeyPattern.exec(config)) !== null) {
    extraApiKeys[envMatch[1]] = envMatch[2];
  }
  values.__extraApiKeys = extraApiKeys;
  return values;
}

function isDefaultBundledApi(url) {
  try {
    const parsed = new URL(url);
    const port = parsed.port || (parsed.protocol === "https:" ? "443" : "80");
    return parsed.protocol === "http:" && ["127.0.0.1", "localhost"].includes(parsed.hostname) && port === "8000";
  } catch {
    return false;
  }
}

function leaApiEnv(config) {
  const env = {
    ...process.env,
    LEA_API_HOST: "127.0.0.1",
    LEA_API_PORT: "8000",
  };
  const mappings = {
    google_api_key: "GOOGLE_API_KEY",
    anthropic_api_key: "ANTHROPIC_API_KEY",
    openai_api_key: "OPENAI_API_KEY",
    openai_base_url: "OPENAI_BASE_URL",
  };
  for (const [configKey, envKey] of Object.entries(mappings)) {
    if (config[configKey]) {
      env[envKey] = config[configKey];
    }
  }
  for (const [envKey, value] of Object.entries(config.__extraApiKeys || {})) {
    if (value) {
      env[envKey] = value;
    }
  }
  if (config.lea_api_key && !env.LEA_API_KEYS) {
    env.LEA_API_KEYS = config.lea_api_key;
  }
  return env;
}

function waitFor(url, label, timeoutMs = 20000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const request = http.get(url, (response) => {
        response.resume();
        resolve();
      });
      request.setTimeout(1000, () => {
        request.destroy();
      });
      request.on("error", () => {
        if (Date.now() - started > timeoutMs) {
          reject(new Error(`${label} did not become ready at ${url}`));
          return;
        }
        setTimeout(tick, 400);
      });
    };
    tick();
  });
}

function portOpen(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port });
    socket.setTimeout(750);
    socket.on("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.on("timeout", () => {
      socket.destroy();
      resolve(false);
    });
    socket.on("error", () => resolve(false));
  });
}

async function ensurePortAvailable(port, label) {
  if (await portOpen(port)) {
    fail(`${label} port ${port} is already in use. Stop the existing process and run npm run dev again.`);
  }
}

function start(label, command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: root,
    stdio: ["ignore", "pipe", "pipe"],
    env: process.env,
    ...options,
  });
  children.push(child);

  child.stdout.on("data", (chunk) => process.stdout.write(`[${label}] ${chunk}`));
  child.stderr.on("data", (chunk) => process.stderr.write(`[${label}] ${chunk}`));
  child.on("exit", (code) => {
    if (code !== 0 && !shuttingDown) {
      console.error(`[dev] ${label} exited with ${code}`);
      shutdown(1);
    }
  });
  return child;
}

let shuttingDown = false;
function shutdown(code = 0) {
  shuttingDown = true;
  for (const child of children) {
    child.kill("SIGINT");
  }
  setTimeout(() => process.exit(code), 200);
}

process.on("SIGINT", () => shutdown());
process.on("SIGTERM", () => shutdown());

ensure("node_modules", "node_modules is missing. Run npm install.");
ensure("config/lea.local.toml", "config/lea.local.toml is missing. Copy config/lea.local.example.toml.");
ensure("server/.venv/bin/python", "server virtualenv is missing. Run npm run setup:api.");
ensure("external/lea-prover/pyproject.toml", "external/lea-prover is missing. Run git submodule update --init --recursive.");

const nodeMajor = Number(process.versions.node.split(".")[0]);
if (nodeMajor !== 22 && nodeMajor !== 20) {
  console.warn(`[dev] Node ${process.versions.node} detected. Node 22 LTS is recommended.`);
}

const config = readConfigStrings();
const leaApiBaseUrl = (config.lea_api_base_url || defaultLeaApiBaseUrl).replace(/\/$/, "");
if (isDefaultBundledApi(leaApiBaseUrl)) {
  ensure("external/lea-prover/.venv/bin/python", "bundled Lea API virtualenv is missing. Run npm run setup:api.");
  await ensurePortAvailable(8000, "Lea API");
  start("lea-api", "./.venv/bin/python", ["-m", "lea_api"], {
    cwd: path.join(root, "external", "lea-prover"),
    env: leaApiEnv(config),
  });
} else {
  console.log(`[dev] Using external Lea API at ${leaApiBaseUrl}`);
}

try {
  await waitFor(`${leaApiBaseUrl}/v1/healthz`, "Lea API", 30000);
  console.log(`[dev] Lea API ready at ${leaApiBaseUrl}`);
} catch (error) {
  console.error(`[dev] ${error.message}`);
  shutdown(1);
}

await ensurePortAvailable(8001, "UI adapter API");
start("adapter", "./.venv/bin/python", ["run_api.py"], { cwd: path.join(root, "server") });

try {
  await waitFor("http://127.0.0.1:8001/api/health", "UI adapter API");
  console.log("[dev] UI adapter API ready at http://127.0.0.1:8001");
} catch (error) {
  console.error(`[dev] ${error.message}`);
  shutdown(1);
}

await ensurePortAvailable(5173, "frontend");
start("web", path.join(root, "node_modules", ".bin", "vite"), ["--host", "0.0.0.0", "--strictPort"]);

try {
  await waitFor("http://127.0.0.1:5173", "frontend");
  console.log("[dev] Frontend ready at http://localhost:5173");
  console.log("[dev] Press Ctrl+C to stop both servers.");
} catch (error) {
  console.error(`[dev] ${error.message}`);
  shutdown(1);
}
