# MyRetail

MyRetail is a SaaS platform for retail automation in Kazakhstan. ERPNext is the system of record for the MVP, while MyRetail provides a stable API and purpose-built web and POS experiences.

The product requirements and decisions live in Notion. This repository contains executable code and setup instructions.

## Repository layout

```text
apps/
  web/          Next.js web application
services/
  api/          Python/FastAPI gateway
infra/
  erpnext/      ERPNext environment documentation and configuration
docs/           Repository-local technical guides
```

## Prerequisites

- Node.js 20.9 or newer (Node.js 24 is used in CI)
- npm 11
- Python 3.11 or newer
- Docker Desktop 4.78 or newer for the ERPNext environment
- Rust for the future Tauri desktop application

On the current workstation, Docker Desktop is installed in `E:\Docker\app` and its WSL data is stored in `E:\Docker\wsl`.

## Install

```powershell
npm.cmd install
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r services/api/requirements-bootstrap.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e "services/api[dev]"
```

The bootstrap lock upgrades the package installer and build backend before any editable build. It
is cross-platform and hash-verified, while the application locks target Linux x86_64 / CPython 3.11:

- `services/api/requirements-bootstrap.lock` contains pip, setuptools, wheel, Hatchling, and their
  exact build dependencies for local Windows setup;
- `services/api/requirements.lock` contains the hash-locked runtime dependency graph;
- `services/api/requirements-migrations.lock` contains the runtime graph plus the migration-only
  Alembic toolchain;
- `services/api/requirements-dev.lock` also contains test, build, and supply-chain tooling;
- `services/api/sbom.cdx.json` is the reproducible CycloneDX runtime SBOM.
- `services/api/sbom-migrations.cdx.json` is the separate reproducible migration artifact SBOM.

CI installs the development lock with `--require-hashes`, installs the local API package with
`--no-deps --no-build-isolation`, and blocks lock or SBOM drift. A future production API image
must install `requirements.lock` with `--require-hashes` and then install the prebuilt MyRetail API
wheel with `--no-deps`; there is no production API image in this repository yet.

Regenerate artifacts only in the pinned Linux target. The normal update path first installs the
existing development lock, then runs the generator; add `--upgrade` only for an intentional
dependency refresh:

```powershell
docker run --rm --mount "type=bind,source=${PWD},target=/workspace" -w /workspace `
  python:3.11-slim@sha256:baf89808ec37adeaab83cec287adb4a2afa4a11c1d51e961c7ec737877e61af6 `
  sh -c "python -m pip install --require-hashes -r services/api/requirements-dev.lock && python services/api/scripts/supply_chain.py --write"
```

When intentionally changing the pinned supply-chain tool versions themselves, bootstrap those
exact versions in the pinned container first; the generator fails closed on a tool/version mismatch.

## Run

Web application:

```powershell
npm.cmd run dev:web
```

API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn myretail_api.main:app --app-dir services/api/src --reload
```

## Validate

```powershell
npm.cmd run lint
npm.cmd run typecheck
.\.venv\Scripts\python.exe -m pytest services/api/tests
.\.venv\Scripts\python.exe -m ruff check services/api
```

## Working agreement

- Keep changes small enough for review.
- Every pull request must pass lint, type checking, tests, and build.
- Never commit credentials, ERPNext API keys, tokens, or tenant secrets.
- Update the relevant Notion page when requirements, architecture, or behavior changes.
