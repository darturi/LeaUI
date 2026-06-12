# Installing & Running Lea (Docker, macOS)

This guide covers running the Lea Interface as a single self-contained Docker
container on an Apple Silicon Mac. It also doubles as an engineering log: every
problem hit while building the container is recorded below with its fix, plus
notes on making a plain `git clone` install seamless for future contributors.

> **Audience.** The "Quick start" section is written for non-technical users
> (e.g. PIs). Everything after it is for developers maintaining the repo.

---

## Quick start (for end users)

You need **Docker Desktop** and **one LLM API key** (Google Gemini, Anthropic,
or OpenAI). You do **not** edit any file — the key goes in the app itself.

1. **Install Docker Desktop** — <https://www.docker.com/products/docker-desktop/>.
   Open it once and wait until the whale icon says *running*.
2. **Get the project folder** (your maintainer will share a download or a
   `git clone` command).
3. **Start it.** Double-click **`start-lea.command`** in Finder.
   - The first launch downloads Lean + Mathlib and builds the image. This is a
     large, one-time download and can take a while (multi-GB).
   - When it's ready, your browser opens to <http://localhost:8001>.
4. **Add your API key in the app.** Open **Settings**, pick your model, paste the
   matching API key, and save. The app validates the key against the provider and
   tells you immediately if it's wrong. Your key is saved locally and persists
   across restarts.
5. **Stop it** by closing the Terminal window the launcher opened, or pressing
   `Ctrl+C` there.

The app starts fine with no key — you'll just be prompted to add one in Settings
before running a proof.

---

## What the container actually runs

One container, one exposed port (`8001`). Inside it:

| Process | Port | Role |
|---|---|---|
| Bundled **Lea API** | `127.0.0.1:8000` (internal) | runs the Lean 4 prover agent |
| **UI adapter** (FastAPI) | `0.0.0.0:8001` (exposed) | normalizes Lea API events into the UI's SSE stream **and** serves the built frontend (same origin) |

The adapter and Lea API share a filesystem (the adapter reads the `.lean` files
the agent writes), which is why they live in the same container. Lean v4.29.0 +
Mathlib v4.29.0 oleans are baked into the image so proofs compile immediately.

Two host folders are mounted as volumes so nothing is lost on restart:
- `./data` — sessions, proof history (SQLite), Lea API event logs.
- `./config` — settings + your API key, written by the in-app Settings pane to
  `config/lea.local.toml`.

### Configuration — all in the app

Configuration is done in the **Settings pane** inside the app (model, API keys,
max turns, spend cap, approval mode). Saving writes `config/lea.local.toml` in the
mounted `./config` volume; the adapter reads it on each run and forwards the key
to the Lea API. There is no key file to edit and no environment variable to set —
this is why the container starts keyless.

Local (non-Docker) dev works the same way via `npm run dev` (three processes,
frontend on Vite `:5173`), reading/writing the same `config/lea.local.toml`.

---

## Build & run manually (developers)

```bash
git submodule update --init --recursive          # pulls external/lea-prover (Lean agent + API)
cp lea.env.example lea.env                        # then edit lea.env: set your key
docker compose up --build                         # builds the image, starts the container
# open http://localhost:8001
```

Or build the image directly:

```bash
docker buildx build --platform linux/arm64 -t lea-interface:local --load .
```

---

## Problems faced & fixes (build log)

A running record so future builds (and other architectures) don't rediscover
these the hard way.

### 1. `lake build` fails: `no such file or directory: proofs/Lea.lean`
- **Symptom.** During the image build, after Mathlib downloaded successfully,
  `lake build` failed with `error: no such file or directory ... proofs/Lea.lean`.
- **Cause.** The workspace `lakefile.lean` declares
  `@[default_target] lean_lib Lea where srcDir := "proofs"`. `lake build` builds
  that default target, which expects a root module `proofs/Lea.lean`. But
  `proofs/` is where the **agent writes proof files at runtime** — it's empty at
  build time, so the default target has no root and the build fails.
- **Fix.** Don't `lake build` the empty workspace lib at all. We only need
  Mathlib's prebuilt oleans, which `lake exe cache get` already places. The
  Dockerfile now runs `lake exe cache get` and asserts the Mathlib olean
  directory is non-empty, skipping `lake build`. The agent compiles individual
  proof files at runtime via `lean_check` (`lake env lean`), which doesn't need
  the default target built.

### Confirmed working
- **Mathlib cache is cross-platform.** `lake exe cache get` downloaded all 8232
  Mathlib oleans for `linux/arm64` from the upstream cache — no need to compile
  Mathlib from source (which would take hours and lots of RAM). Lean oleans are
  architecture-independent, so the same cache serves arm64 Linux containers.
- **elan + Lean v4.29.0 install cleanly** on `linux/arm64` via the upstream
  `elan-init.sh`.

### 2. `docker compose up` with no key file dies with a cryptic error
- **Symptom.** A brand-new user who runs `docker compose up` *before* creating
  `lea.env` gets: `env file ... lea.env not found` and the container never
  starts. No hint about what `lea.env` is or that they need an API key.
- **Cause.** `docker-compose.yml` referenced `env_file: [lea.env]`, which Compose
  treats as required and validates *before* starting the container — so our nice
  in-container preflight message never runs.
- **Fix.** Mark the env file optional:
  ```yaml
  env_file:
    - path: lea.env
      required: false
  ```
  Now the container always starts and the entrypoint's preflight prints a clear,
  numbered "create lea.env and paste your key" message, then exits cleanly. The
  entrypoint message was also reworded to include the `cp lea.env.example lea.env`
  step in case the file doesn't exist yet.
- **Verified.** Cold start with no `lea.env` now prints:
  ```
  Lea cannot start: no API key found.
  Model 'gemini/gemini-3.1-pro-preview' needs the 'GOOGLE_API_KEY' key.
   1. If you haven't yet:  cp lea.env.example lea.env
   2. Open 'lea.env' ... and paste your key after 'GOOGLE_API_KEY='
   3. Save and start Lea again.
  ```

### 3. Merging the `settings_pane` feature changed the key model (for the better)
- **Context.** The Docker work was started on `ui_api_integration` before the
  `settings_pane` branch (in-app Settings pane, live key validation, spend cap,
  approval tiers) was merged into it upstream. After fast-forwarding the local
  branch to the merged remote, the Docker packaging had to be reconciled with it.
- **What changed.** The merged adapter **forwards the API key to the Lea API per
  run** (`lea_api_client.py` sends `{"api_key": ...}` from `config/lea.local.toml`).
  So the key no longer needs to be in any process environment — it flows
  Settings pane → `config/lea.local.toml` → adapter → Lea API.
- **Consequences for Docker (all simplifications).**
  - Dropped `lea.env` and the env-key plumbing entirely. The container starts
    **keyless**; the user adds the key in the Settings pane.
  - Removed the entrypoint's API-key preflight (it used to block startup). With
    keyless start, blocking would prevent the user from ever reaching Settings.
  - Mounted `./config` as a volume so the in-app key/settings persist across
    restarts (previously only `./data` was mounted).
  - Re-applied the same-origin static-serving change to the merged `main.py`
    (the merge didn't include it), so `:8001` serves the built frontend.
  - Synced the submodule to the merged pointer (`070eea1`, branch
    `thrm_permission`) and let `uv sync` pick up the new `certifi` dependency.
- **Takeaway.** One Dockerfile builds whatever branch is checked out; there is no
  per-branch image. Consolidating onto the merged branch = one image again.

<!-- Add new problems here as they come up, newest first. -->

---

## Making `git clone` seamless for future CS users

Ideas to reduce onboarding friction (not all implemented yet):

- **One env file, not two config systems.** Today local dev uses
  `config/lea.local.toml` while Docker uses `lea.env`. Consider standardizing on
  env vars everywhere (the adapter already supports them) so there's a single
  thing to set.
- **Fail fast on a missing key in local dev too.** The Docker entrypoint
  preflights the API key and exits with a clear message. `scripts/dev.mjs` does
  not yet — a missing key currently surfaces only on first theorem submission.
  Port the same preflight into `dev.mjs`.
- **Pre-built image for the team.** Building locally downloads multi-GB of
  Mathlib. Publish the image to a registry (GHCR) and have `docker-compose.yml`
  default to `pull` so most users never build. Tag images by Lean/Mathlib
  version.
- **Pin Node in the toolchain.** `dev.mjs` only *warns* on Node ≠ 20/22. Document
  (or enforce via `.nvmrc` / `engines`) the supported Node for contributors who
  run the frontend outside Docker.
- **Multi-arch image.** This build targets `linux/arm64` only. Add `linux/amd64`
  via `docker buildx build --platform linux/arm64,linux/amd64` so Intel Macs and
  Linux boxes work from the same tag.
- **Submodule reminder.** A clone without `--recurse-submodules` leaves
  `external/lea-prover` empty and every build/run fails confusingly. The
  Dockerfile assumes the submodule is present; a top-level preflight (or a
  `make` target) that runs `git submodule update --init --recursive` would help.
- **Shrink the image (partly done).** It bakes Mathlib oleans + two venvs + Lean
  toolchain, so it's large. An in-layer cleanup of the `lake exe cache get` step
  (`rm -rf .lake/packages/*/.git` + `/root/.cache/mathlib`) took it from
  **15.7 GB → 13.9 GB**. The remaining ~8.2 GB (Lean toolchain 2.7 GB + Mathlib
  oleans 5.5 GB) is irreducible while Mathlib is baked in. The only way lower is
  to *not* bake Mathlib (image ~3 GB) and `cache get` on first container start —
  trades a smaller pull for a ~5.5 GB first-run download.
