# CodeRevive — Hybrid OpenRewrite + LLM Spring Boot Migration Agent

CodeRevive is an autonomous software engineering agent that migrates legacy **Spring Boot 2.x (Java 8/11)** Maven applications to **Spring Boot 3.x (Java 17)** using a hybrid architecture:

1. **Deterministic AST & Recipe Transformation (OpenRewrite)** for mechanical, high-confidence upgrades (`pom.xml` BOM alignment, Jakarta EE `javax.*` -> `jakarta.*` allowlist, Thymeleaf `spring5` -> `spring6`, `LocalServerPort`).
2. **Structured AI Agent & Iterative Repair Loop (Gemini / NVIDIA NIM)** for complex architectural refactorings (`WebSecurityConfigurerAdapter` -> `SecurityFilterChain`, Thymeleaf `#request` templates, YAML/properties updates) and diagnostic-driven repair.
3. **Docker-Isolated Sandbox & Pre/Post Guardrails** to execute Maven builds, classify environment vs. code failures, validate patch targets, verify public method signatures and test preservation *before* committing, and automatically roll back on any violation.

---

## Architecture

```
Repository (Git URL or local path)
   │
   ▼
[1. Ingestion & Planning]
   ├── GitWorkspaceManager  ──▶ Isolated branch & workspace copy (never mutates original repo)
   ├── PomParser (lxml)     ──▶ Dynamic MigrationPlan (target Boot 3.x, Java 17, BOM overrides)
   ├── JavaAstParser        ──▶ Tree-sitter AST (full method signatures, test bodies, Jakarta allowlist)
   └── DependencyGraph      ──▶ NetworkX topological migration order + blast-radius analysis
   │
   ▼
[2. Hybrid Transformation]
   ├── Stage A: OpenRewriteRunner ──▶ Maven rewrite-maven-plugin (or deterministic AST/POM fallback)
   └── Stage B: LLM Pass 1        ──▶ Structured FilePatchResult ONLY for residual complex targets
   │
   ▼
[3. Sandboxed Verification & Targeted Repair]
   ├── ServiceDetector      ──▶ Detects MySQL, PostgreSQL, Redis, MongoDB from pom.xml + app config
   ├── DockerServiceManager ──▶ Boots ephemeral sidecar containers in a shared pod/bridge network
   ├── DockerRunner (hardened: cap-drop=ALL, no-new-privileges, pids-limit, mem/cpu/timeout)
   ├── DiagnosticsParser    ──▶ Classifies environment vs. code failures; groups errors by file
   └── Scoped LLM Repair    ──▶ Fixes only implicated files within retry budget
   │
   ▼
[4. Pre-Commit Integrity Guardrails]
   ├── Pre-Execution  ──▶ Exact target path match, path-traversal block, forbidden API patterns
   ├── Post-Execution ──▶ Test count >= baseline, full public method signature diff, non-empty tests
   └── Git Commit / Rollback ──▶ Commits ONLY if build, per-file states, and integrity all pass
```

---

## Key Engineering Guarantees

- **Repository-Independent Migration Plans**: Dynamically generated from parsed `pom.xml` metadata (`MigrationPlan`) with configurable `--target-boot` and `--target-java`.
- **Dynamic Ephemeral Backing Services**: Automatically provisions matching Docker sidecar containers (`mysql:8.0`, `postgres:16-alpine`, `redis:7-alpine`, `mongo:7`) with health-readiness probes in an isolated shared network whenever a repository's configuration or `pom.xml` requires them, tearing them down immediately after verification (with non-destructive test-profile/H2 fallback if a container cannot be provisioned).
- **Safe Jakarta EE Allowlist**: Migrates Jakarta EE namespaces (`javax.persistence`, `javax.validation`, `javax.servlet`, `javax.annotation`, `javax.transaction`, etc.) while explicitly preserving Java SE packages (`javax.sql.*`, `javax.crypto.*`, `javax.net.*`, `javax.xml.parsers.*`).
- **No False-Success Reporting**: Explicit per-file state machine (`PENDING -> GENERATING -> PATCH_APPLIED -> VALIDATED -> SUCCESS`, or `FAILED / REJECTED / SKIPPED`). Post-execution integrity checks run **before** `git commit`, preventing broken migrations from ever being committed.
- **Strict Target-Path Enforcement**: LLM patches targeting any path other than the assigned file raise `SecurityViolationError`, even if the path lies inside the repository.

---

## Setup & Configuration

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env` with your API key (`CODEREVIVE_GEMINI_API_KEY` or `CODEREVIVE_NVIDIA_API_KEY`) and set the target repository in **one place**:

```env
CODEREVIVE_REPO_URL=https://github.com/kazuki43zoo/mybatis-spring-boot-jpetstore.git
```

To build the hardened Maven + Java 17 sandbox image:

```bash
docker build -f backend/Dockerfile.sandbox -t coderevive-maven-chrome:17 backend
```

---

## Usage

Run an end-to-end migration via CLI (reads `CODEREVIVE_REPO_URL` from `backend/.env` by default, or pass `--repo-url` to override):

```bash
python backend/app/orchestrator/run_migration.py \
  --target-boot 3.2.5 \
  --target-java 17
```

Or override for a single run:

```bash
python backend/app/orchestrator/run_migration.py \
  --repo-url https://github.com/sinsangkug/SpringBoot_Jpa_Shopping \
  --target-boot 3.2.5 \
  --target-java 17
```

Useful flags:
- `--openrewrite-mode` — `hybrid` (default), `deterministic` (instant local AST/POM pass), or `maven_only`.
- `--no-openrewrite` — disable the deterministic/OpenRewrite pre-pass and route all targets through the LLM.
- `--retry-budget N` — maximum sandbox repair rounds (default: `3`).
- `--clean` — delete and re-clone/re-copy the isolated workspace.

For programmatic or frontend integration, run the FastAPI backend server (`python backend/app/main.py` or `uvicorn app.main:app --host 0.0.0.0 --port 8000`) exposing interactive OpenAPI docs at `/docs` and endpoints `GET /api/config`, `GET /api/migrations/latest`, `POST /api/migrations`, and background job tracking at `/api/migrations/jobs`.

---

## Automated Tests & Evaluation Benchmark

Run the full `pytest` unit and integration suite (covering AST parsing, POM planning, Jakarta allowlist, OpenRewrite runner, guardrails, diagnostics classification, sandbox DB fallback, and orchestrator failure/rollback states):

```bash
pytest backend/tests -v
```

Run the multi-repository evaluation benchmark across the three Spring Boot 2.7 fixtures (`basic-crud`, `security-jwt`, `thymeleaf-web`):

```bash
python backend/app/orchestrator/benchmark.py
```

### Measured Benchmark Results (Offline Deterministic Mock LLM + Real AST/Guardrails)

Generated by `backend/app/orchestrator/benchmark.py` and saved to `benchmarks/latest_benchmark.json`:

| Project | Source -> Target | Targets | OpenRewrite Handled | LLM Handled | Integrity Passed | Final State |
|---|---|---:|---:|---:|---|---|
| `spring-boot-2.7-basic` | `2.7.14 -> 3.2.5` | 3 | 3 | 0 | Yes | `COMPLETED` |
| `spring-boot-2.7-security` | `2.7.18 -> 3.2.5` | 3 | 2 | 1 | Yes | `COMPLETED` |
| `spring-boot-2.7-thymeleaf` | `2.7.10 -> 3.2.5` | 4 | 2 | 2 | Yes | `COMPLETED` |

See `benchmarks/sample_audit_report.json` for a complete migration audit report including per-file unified diffs, commit SHAs, and integrity verification results.

---

## Supported Scope & Current Limitations

- **Supported**: Single-module Maven Spring Boot 2.x -> 3.x projects with standard `src/main/java`, `src/test/java`, and `src/main/resources` layouts.
- **Unsupported (explicitly rejected in MVP)**: Multi-module Maven projects (`<modules>` in root `pom.xml`) and Gradle builds.
- **Note on Sandbox & Evaluation**: The CI test suite and default benchmark runner use a deterministic mock LLM and simulated build output to verify orchestration, AST analysis, OpenRewrite transforms, and guardrails deterministically without external API keys. End-to-end compilation of migrated Java projects requires Docker with `coderevive-maven-chrome:17`.