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
.\.venv\Scripts\python.exe -m pip install -e "services/api[dev]"
```

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
