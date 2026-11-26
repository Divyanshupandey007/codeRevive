<!-- prettier-ignore -->
<div align="center">

<img src="./frontend/public/favicon.png" alt="CodeRevive logo" width="96" />

# CodeRevive

*Autonomous Spring Boot 2.x to 3.x migration agent powered by OpenRewrite, structured LLMs, and hardened Docker sandboxes*

[![Migration Workflow](https://img.shields.io/github/actions/workflow/status/Divyanshupandey007/codeRevive/coderevive-migrate.yml?style=flat-square&label=Migration%20Workflow)](https://github.com/Divyanshupandey007/codeRevive/actions/workflows/coderevive-migrate.yml)
![Python >= 3.11](https://img.shields.io/badge/Python-%3E%3D3.11-3776ab?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Angular 21](https://img.shields.io/badge/Angular-21-dd0031?style=flat-square&logo=angular&logoColor=white)
![Spring Boot 2.x to 3.x](https://img.shields.io/badge/Spring%20Boot-2.x%20%E2%86%92%203.x-6db33f?style=flat-square&logo=springboot&logoColor=white)
![Docker Sandbox](https://img.shields.io/badge/Sandbox-Docker-2496ed?style=flat-square&logo=docker&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-f59e0b?style=flat-square)](LICENSE)

[Features](#features) • [Architecture](#architecture) • [Quick Start](#quick-start) • [Usage](#usage) • [Deployment](#deployment) • [Testing](#testing-and-benchmarks)

</div>

CodeRevive automates upgrading legacy **Spring Boot 2.x (Java 8/11)** Maven applications to **Spring Boot 3.x (Java 17)**. Rather than relying solely on unconstrained LLM edits, it combines deterministic AST/OpenRewrite recipes for mechanical upgrades with diagnostic-driven LLM refactoring inside a hardened Docker build sandbox—enforcing strict pre- and post-execution integrity checks before committing a single change.

> [!NOTE]
> The `pytest` test suite and offline evaluation benchmark run deterministically without Docker or external LLM API keys. Full end-to-end compilation of migrated Java repositories requires Docker with the `coderevive-maven-chrome:17` sandbox image.

## Features

- **Hybrid Deterministic + AI Pipeline**: Runs OpenRewrite (`rewrite-maven-plugin` or a fast local AST/POM fallback) for high-confidence mechanical transforms (`pom.xml` BOM alignment, Jakarta EE namespaces, Thymeleaf `spring5` → `spring6`, `LocalServerPort`) and invokes LLMs only for complex architectural refactorings (`WebSecurityConfigurerAdapter` → `SecurityFilterChain`, template and configuration updates).
- **Multi-Provider LLM Routing**: Supports **Google Gemini** (`gemini-2.5-flash`), **OpenRouter** (`openai/gpt-oss-120b`), and **NVIDIA NIM** (`mistralai/mistral-large-2-instruct`) with bounded concurrent repair waves across independent files.
- **Hardened Docker Sandbox & Dynamic Sidecars**: Builds and tests migrated code inside an isolated container (`cap-drop=ALL`, `no-new-privileges`, memory/CPU/PID limits, Chrome/ChromeDriver for browser tests). Automatically provisions ephemeral `mysql:8.0`, `postgres:16-alpine`, `redis:7-alpine`, or `mongo:7` sidecars when detected in `pom.xml` or app config, tearing them down after verification (with a non-destructive H2/test-profile fallback).
- **Pre-Commit Integrity Guardrails**: Applies a strict Jakarta EE allowlist (preserving Java SE `javax.sql.*`, `javax.crypto.*`, `javax.net.*`, `javax.xml.parsers.*`), rejects out-of-target file writes (`SecurityViolationError`) and forbidden APIs (`Runtime.getRuntime().exec`, `ProcessBuilder`, `System.exit`), verifies public method signatures and baseline test counts before `git commit`, and rolls back on any failure.
- **Angular 21 Migration Console**: Web interface with a job Launcher, real-time Pipeline Monitor, and Audit Report featuring `diff2html` unified diff inspection and artifact downloads.
- **Flexible Execution Modes**: Run migrations locally in Docker (`local`) or dispatch them asynchronously to GitHub Actions `ubuntu-24.04` runners (`github_actions`) for stateless production hosting.

## Architecture

```text
Repository (Git URL or local path)
   │
   ▼
[1. Ingestion & Planning]
   ├── GitWorkspaceManager  ──▶ Isolated branch & workspace copy (never mutates original repo)
   ├── PomParser (lxml)     ──▶ Dynamic MigrationPlan (target Boot 3.x, Java 17, BOM overrides)
   ├── JavaAstParser        ──▶ Tree-sitter AST (method signatures, test bodies, Jakarta allowlist)
   └── DependencyGraph      ──▶ NetworkX topological order & blast-radius analysis
   │
   ▼
[2. Hybrid Transformation]
   ├── Stage A: OpenRewrite ──▶ Maven rewrite-maven-plugin (or deterministic AST/POM fallback)
   └── Stage B: LLM Pass 1  ──▶ Structured FilePatchResult for residual complex targets only
   │
   ▼
[3. Sandboxed Verification & Targeted Repair]
   ├── ServiceDetector      ──▶ Detects MySQL, PostgreSQL, Redis, MongoDB from pom.xml & config
   ├── DockerServiceManager ──▶ Boots ephemeral sidecar containers on a shared bridge network
   ├── DockerRunner         ──▶ Hardened Maven + Java 17 + Chrome sandbox verification
   ├── DiagnosticsParser    ──▶ Classifies environment vs. code failures; groups errors by file
   └── Scoped LLM Repair    ──▶ Fixes implicated files within retry budget
   │
   ▼
[4. Pre-Commit Integrity Guardrails]
   ├── Pre-Execution        ──▶ Exact target-path check, traversal block, forbidden API scan
   ├── Post-Execution       ──▶ Test count >= baseline, public method signature diff, non-empty tests
   └── Commit / Rollback    ──▶ Commits ONLY if build, per-file states, and integrity checks pass
```

### Repository Layout

| Path | Description |
| --- | --- |
| `backend/` | FastAPI REST API, migration orchestrator, Tree-sitter AST/POM ingestion, OpenRewrite runner, LLM providers, guardrails, and Docker sandbox runner. |
| `frontend/` | Angular 21 Migration Console (`Launcher`, `Pipeline Monitor`, `Audit Report`, and `Diff Inspector`). |
| `.github/workflows/coderevive-migrate.yml` | `workflow_dispatch` runner for remote sandboxed migrations and artifact uploads (`migration-run-summary`, `migrated-source`). |

## Prerequisites

- **Python 3.11+** (3.12 recommended)
- **Docker** (required for sandboxed Maven builds and dynamic database sidecars)
- **Node.js 20+** and **npm** (required only if running the Angular 21 frontend)
- **LLM API Key** from [Google AI Studio (Gemini)](https://aistudio.google.com/app/apikey), [OpenRouter](https://openrouter.ai/keys), or [NVIDIA NIM](https://build.nvidia.com/)

## Quick Start

### 1. Set up the backend

```bash
git clone https://github.com/Divyanshupandey007/codeRevive.git
cd codeRevive/backend

python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env` to select your LLM provider, add its API key, and set your default target repository:

```env
CODEREVIVE_REPO_URL=https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git
CODEREVIVE_LLM_PROVIDER=gemini
CODEREVIVE_GEMINI_API_KEY=your-gemini-api-key-here
```

> [!TIP]
> You can switch providers at any time by setting `CODEREVIVE_LLM_PROVIDER` to `gemini`, `openrouter`, or `nvidia` along with its corresponding API key. Use `CODEREVIVE_LLM_MODEL` to override the default model for the active provider.

### 2. Build the sandbox Docker image

Build the hardened Maven 3.9 + Eclipse Temurin Java 17 + Chrome/ChromeDriver image from the repository root:

```bash
docker build -f backend/Dockerfile.sandbox -t coderevive-maven-chrome:17 backend
```

### 3. Run the API server and frontend (optional)

Start the FastAPI backend (Swagger UI available at `http://127.0.0.1:8000/api/docs`):

```bash
python backend/app/main.py
```

In a separate terminal, start the Angular 21 Migration Console (proxies `/api` to `http://127.0.0.1:8000`):

```bash
cd frontend
npm install
npm start
```

Open `http://localhost:4200` to launch migrations, monitor real-time pipeline stages, and inspect unified diffs.

## Usage

### CLI Migration

Run an end-to-end migration using the default `CODEREVIVE_REPO_URL` from `backend/.env`:

```bash
python backend/app/orchestrator/run_migration.py \
  --target-boot 3.2.5 \
  --target-java 17
```

Override the repository and workspace state for a single run:

```bash
python backend/app/orchestrator/run_migration.py \
  --repo-url https://github.com/sinsangkug/SpringBoot_Jpa_Shopping \
  --target-boot 3.2.5 \
  --target-java 17 \
  --clean
```

#### CLI Options

```text
python backend/app/orchestrator/run_migration.py [OPTIONS]

Options:
  --repo-url <url>             Target Git URL or local repository path (default: CODEREVIVE_REPO_URL)
  --target-boot <version>      Target Spring Boot 3.x version          (default: 3.2.5)
  --target-java <version>      Target Java version                     (default: 17)
  --branch <name>              Isolated migration branch name          (default: migration/spring-boot-3)
  --openrewrite-mode <mode>    hybrid | deterministic | maven_only     (default: hybrid)
  --no-openrewrite             Skip OpenRewrite pre-pass and route all targets to the LLM
  --retry-budget <N>           Maximum sandbox repair rounds           (default: 5)
  --clean                      Delete and re-clone/re-copy the isolated workspace before running
```

### REST API Reference

The backend exposes interactive OpenAPI docs at `/api/docs` and ReDoc at `/api/redoc`.

<details>
<summary>View core REST API endpoints</summary>

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness health check (`/api/health/live` and `/api/health/ready` also available). |
| `GET` | `/api/config` | Active non-secret backend configuration (provider, target versions, execution mode). |
| `POST` | `/api/migrations` | Run a migration synchronously (`200`) or asynchronously via `?async=true` / `Prefer: respond-async` (`202`). |
| `GET` | `/api/migrations/latest` | Retrieve the latest completed migration audit summary. |
| `GET` | `/api/migrations/jobs` | List asynchronous migration jobs (filterable by `status`, `limit`, `offset`). |
| `POST` | `/api/migrations/jobs` | Queue and start an asynchronous migration job. |
| `GET` | `/api/migrations/jobs/{job_id}` | Fetch job status, stage progress, and result payload. |
| `GET` | `/api/migrations/jobs/{job_id}/events` | Fetch ordered pipeline stage events for a job. |
| `POST` | `/api/migrations/jobs/{job_id}/cancel` | Request cancellation of a queued or running job. |
| `DELETE` | `/api/migrations/jobs/{job_id}` | Delete a completed or failed job record. |
| `GET` | `/api/migrations/jobs/{job_id}/artifacts` | List workflow artifacts for a GitHub Actions job. |
| `GET` | `/api/migrations/jobs/{job_id}/artifacts/{artifact_id}/download` | Download a workflow artifact (`migration-run-summary` or `migrated-source`). |

</details>

### Environment Variables

All configuration uses the `CODEREVIVE_` prefix (see [`backend/.env.example`](backend/.env.example) for the complete template).

<details>
<summary>View key environment variables</summary>

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODEREVIVE_REPO_URL` | `mybatis-spring-boot-jpetstore` | Default target repository URL. |
| `CODEREVIVE_TARGET_SPRING_BOOT_VERSION` | `3.2.5` | Target Spring Boot 3.x version. |
| `CODEREVIVE_TARGET_JAVA_VERSION` | `17` | Target Java version. |
| `CODEREVIVE_OPENREWRITE_MODE` | `hybrid` | OpenRewrite strategy (`hybrid`, `deterministic`, or `maven_only`). |
| `CODEREVIVE_DEFAULT_RETRY_BUDGET` | `5` | Maximum sandbox build/repair rounds. |
| `CODEREVIVE_LLM_PROVIDER` | `gemini` | Active LLM provider (`gemini`, `openrouter`, or `nvidia`). |
| `CODEREVIVE_LLM_MODEL` | *(provider default)* | Optional explicit model override across providers. |
| `CODEREVIVE_LLM_MAX_CONCURRENCY` | `4` | Max concurrent LLM repair calls per dependency wave. |
| `CODEREVIVE_SANDBOX_MEMORY_LIMIT` | `4g` | Memory cap for sandbox containers. |
| `CODEREVIVE_SANDBOX_DYNAMIC_SERVICES` | `true` | Auto-provision ephemeral DB/cache sidecars when needed. |
| `CODEREVIVE_API_HOST` / `CODEREVIVE_API_PORT` | `127.0.0.1` / `8000` | FastAPI server bind host (`0.0.0.0` in Docker) and port. |
| `CODEREVIVE_API_KEY` | *(empty)* | Optional `X-API-Key` header required for state-changing routes. |
| `CODEREVIVE_CORS_ALLOWED_ORIGINS` | `http://localhost:4200,...` | Comma-separated CORS origins for the Angular frontend. |
| `CODEREVIVE_EXECUTION_MODE` | `local` | Execution mode: `local` (Docker) or `github_actions` (remote runners). |
| `CODEREVIVE_MAX_CONCURRENT_GITHUB_ACTIONS_JOBS` | `1` | Cap on concurrent dispatched GitHub Actions migration jobs. |

</details>

## Deployment

CodeRevive supports a three-tier production architecture:

1. **Frontend** — Angular 21 static app hosted on [Cloudflare Pages](https://pages.cloudflare.com). Communicates exclusively with the FastAPI REST API.
2. **Backend** — Containerized FastAPI server (`backend/Dockerfile`) deployed on **Hostless** (`CODEREVIVE_API_HOST=0.0.0.0`, port `8000` or platform `$PORT`).
3. **Migration Execution** — Ephemeral GitHub Actions `ubuntu-24.04` runners triggered via `.github/workflows/coderevive-migrate.yml` (`workflow_dispatch`).

### Execution Modes

| Mode (`CODEREVIVE_EXECUTION_MODE`) | Behavior | Best For |
| --- | --- | --- |
| `local` *(default)* | Launches `coderevive-maven-chrome:17` directly via the host Docker daemon. | Local development and single-host Docker setups. |
| `github_actions` | Dispatches `coderevive-migrate.yml` via GitHub API, tracks the run as `gha_<run_id>`, relays or polls live progress events, and serves `migration-run-summary` / `migrated-source` artifacts (`retention-days: 1`). Synchronous `POST /api/migrations` returns `400`. | Production deployments without local Docker-in-Docker access. |

### Required GitHub Secrets

| Secret | Scope / Purpose |
| --- | --- |
| `CODEREVIVE_GEMINI_API_KEY` | Google Gemini API key (when using `gemini`). |
| `CODEREVIVE_OPENROUTER_API_KEY` | OpenRouter API key (when using `openrouter`). |
| `CODEREVIVE_NVIDIA_API_KEY` | NVIDIA NIM API key (when using `nvidia`). |
| `CODEREVIVE_GITHUB_TOKEN` | Backend-only PAT or GitHub App token with `actions:write` to dispatch, poll, cancel, and fetch artifacts. |
| `CODEREVIVE_PROGRESS_CALLBACK_TOKEN` | *(Optional)* Shared bearer token for real-time stage relay callbacks (`POST /api/internal/github-actions/runs/{run_id}/events`). |

> [!WARNING]
> Never expose `CODEREVIVE_GITHUB_TOKEN` or LLM provider API keys to the Angular frontend. All provider credentials must remain in backend environment variables or GitHub Actions Secrets.

## Testing and Benchmarks

Run the backend unit and integration test suite (covering AST parsing, POM planning, Jakarta allowlist safety, OpenRewrite execution, guardrails, diagnostics classification, dynamic sidecars, API routing, and orchestrator rollback states):

```bash
pytest backend/tests -v
```

Run the Angular frontend test suite:

```bash
cd frontend && npm test
```

Run the multi-repository evaluation benchmark across the three Spring Boot 2.7 test fixtures in `backend/tests/fixtures/`:

```bash
python backend/app/orchestrator/benchmark.py
```

### Offline Benchmark Results

Measured with the deterministic mock LLM paired with real Tree-sitter AST analysis, OpenRewrite transforms, and pre/post integrity guardrails:

| Fixture | Source → Target | Targets | OpenRewrite Handled | LLM Handled | Integrity Passed | Final State |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `spring-boot-2.7-basic` | `2.7.14 → 3.2.5` | 3 | 3 | 0 | Yes | `COMPLETED` |
| `spring-boot-2.7-security` | `2.7.18 → 3.2.5` | 3 | 2 | 1 | Yes | `COMPLETED` |
| `spring-boot-2.7-thymeleaf` | `2.7.10 → 3.2.5` | 4 | 2 | 2 | Yes | `COMPLETED` |

## Scope and Limitations

> [!IMPORTANT]
> CodeRevive currently targets **single-module Maven** Spring Boot 2.x → 3.x projects with standard `src/main/java`, `src/test/java`, and `src/main/resources` layouts. Multi-module Maven projects (`<modules>` in root `pom.xml`) and Gradle builds are explicitly rejected in the current release.

## License

Released under the [MIT License](LICENSE).