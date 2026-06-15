# syntax=docker/dockerfile:1

# =============================================================================
# Lea Interface — self-contained image (frontend + UI adapter + bundled Lea API
# + Lean 4 + Mathlib). Built for linux/arm64 (Apple Silicon Macs).
#
# Build:  docker buildx build --platform linux/arm64 -t lea-interface:local --load .
# Run:    docker compose up
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: build the React frontend into static assets (dist/)
# -----------------------------------------------------------------------------
FROM node:22-bookworm-slim AS web
WORKDIR /web

COPY package.json package-lock.json ./
RUN npm ci

# Only the files the Vite build actually reads (keeps this layer small + cacheable).
COPY index.html vite.config.ts postcss.config.mjs ./
COPY src ./src
RUN npm run build   # -> /web/dist

# -----------------------------------------------------------------------------
# Stage 2: runtime — Python 3.13 + uv (base), plus Lean toolchain and Mathlib
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

# System packages: git (lake fetches Mathlib), curl/ca-certificates (elan +
# health checks), libgmp10 (Lean runtime dependency).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates libgmp10 \
 && rm -rf /var/lib/apt/lists/*

# Install the Lean toolchain manager (elan) and pin the project's Lean version.
ENV ELAN_HOME=/root/.elan
ENV PATH="/root/.elan/bin:${PATH}"
RUN curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o /tmp/elan-init.sh \
 && sh /tmp/elan-init.sh -y --default-toolchain leanprover/lean4:v4.29.0 \
 && rm /tmp/elan-init.sh \
 && lean --version

WORKDIR /app

# --- Bundled Lea agent + API (the submodule) -------------------------------
# Copy the submodule first so the expensive Mathlib bake is its own cache layer.
COPY external/lea-prover ./external/lea-prover

# Bake Mathlib AND build SafeVerify in one layer so they share a SINGLE Mathlib.
#
# Both the workspace and SafeVerify pin the identical Mathlib commit (and the
# same transitive deps), so SafeVerify symlinks the workspace's already-baked
# `.lake/packages` instead of downloading a second multi-GB copy. The image then
# carries exactly one Mathlib. This is one RUN because the .git trim only frees
# space when it happens in the same layer that created the files.
#
# `lake exe cache get` fetches Mathlib's prebuilt oleans (no source compile). We
# do NOT `lake build` the workspace: its @[default_target] `lean_lib Lea` is
# rooted at proofs/, which is empty until the agent writes there at runtime.
# `safe_verify`, by contrast, has its own exe target and builds fine here.
WORKDIR /app/external/lea-prover
RUN cd workspace \
 && lake exe cache get \
 && test -n "$(ls -A .lake/packages/mathlib/.lake/build/lib 2>/dev/null)" \
 && echo "[build] Mathlib oleans present" \
 && cd /app/external/lea-prover/third_party/SafeVerify \
 && rm -rf .lake/packages && mkdir -p .lake \
 && ln -s /app/external/lea-prover/workspace/.lake/packages .lake/packages \
 && lake build safe_verify \
 && test -x .lake/build/bin/safe_verify \
 && echo "[build] SafeVerify built against the shared Mathlib" \
 && rm -rf /app/external/lea-prover/workspace/.lake/packages/*/.git \
 && rm -rf /root/.cache/mathlib \
 && echo "[build] one Mathlib shared; trimmed .git histories + cache"

# Python deps for the agent + Lea API (creates external/lea-prover/.venv).
WORKDIR /app/external/lea-prover
RUN uv sync --extra api

# --- UI adapter (FastAPI) ---------------------------------------------------
WORKDIR /app/server
COPY server ./
RUN uv sync   # creates /app/server/.venv

# --- Built frontend + entrypoint -------------------------------------------
COPY --from=web /web/dist /app/dist
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

# Defaults that make the adapter and Lea API agree on paths/URLs in-container.
# Provider key + model come from the runtime environment (lea.env).
ENV PYTHONUNBUFFERED=1 \
    LEA_API_BASE_URL=http://127.0.0.1:8000 \
    LEA_ROOT=/app/external/lea-prover \
    LEA_WEB_DIST=/app/dist \
    LEA_MODEL=gemini/gemini-3.1-pro-preview

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8001/api/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
