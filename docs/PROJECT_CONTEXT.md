# PROJECT CONTEXT — Execution-Driven Autonomous CI/CD Healing Agent

## Purpose

This document defines the architecture philosophy for the backend.
The system behaves as an **execution-driven CI healing agent**, not a static code analyzer.

---

## Core Idea

The agent simulates a CI pipeline locally, observes runtime behavior, fixes failures, and iterates.

**Errors are discovered by executing the project, not by statically analyzing the repository.**

Static analysis may be used as a helper but **must never drive the workflow**.
Execution is the primary truth source.

---

## Primary Workflow

A single API call triggers the full autonomous loop:

1. Clone repository
2. Prepare environment (Docker sandbox)
3. Detect project type from repository signals
4. Build project using standard commands
5. Run tests automatically
6. Capture compiler and runtime failures
7. Convert failures into structured bug reports
8. Generate targeted fixes using LLM
9. Commit fixes with strict Git rules
10. Push to new branch
11. Monitor GitHub CI/CD
12. If CI fails and retry limit not reached → repeat loop
13. After success or retry exhaustion → generate `results.json`

---

## Local Build vs Remote CI

| Environment | Purpose |
|---|---|
| **Local build** (agent controlled) | Detect failures quickly through execution |
| **Remote CI** (GitHub Actions) | Validation verdict after fixes are pushed |

CI is a verdict, **not** the primary diagnostic source.

---

## Language Agnostic Strategy

The agent supports repositories of any language without deep language-specific logic.

Detection via repository signals:

| Signal File | Project Type |
|---|---|
| `package.json` | Node.js / JavaScript |
| `requirements.txt`, `setup.py`, `pyproject.toml` | Python |
| `pom.xml`, `build.gradle` | Java |
| `go.mod` | Go |
| `Cargo.toml` | Rust |
| `Dockerfile` | Container-based |

Standard build and test commands are derived from project type.
Error extraction from runtime logs is universal across languages.

---

## Bug Classification Layer

Failures are normalized into six allowed bug types:

- `LINTING`
- `SYNTAX`
- `LOGIC`
- `TYPE_ERROR`
- `IMPORT`
- `INDENTATION`

Classification is derived from **runtime error messages and compiler output**.

---

## Absolute Priority Rule (40 Points)

Bug output format must match **exactly**:

```
{BUG_TYPE} error in {file_path} line {line_number} → Fix: {fix_description}
```

- Arrow must be Unicode **U+2192** (`→`)
- Exact string matching required
- **LLM must never generate this string. Only code logic generates it.**

---

## Multi-Agent Architecture

| Agent | Responsibility |
|---|---|
| Orchestrator Agent | Controls the retry loop |
| Build Executor | Prepares environment, builds and runs project |
| Fix Agent | Generates minimal code fixes via LLM |
| Git Agent | Branch, commit, push operations |
| CI Monitor Agent | Polls GitHub Actions pipeline status |

Agents communicate through shared state.

---

## Fix Philosophy

- Fix only reported lines **±3**
- Minimum diff only
- Preserve comments
- Do not refactor unrelated code
- Code must remain runnable

LLM generates code changes, **not** evaluation strings.

---

## Git Rules (Disqualification Risk)

**Branch format:** `TEAM_NAME_LEADER_NAME_AI_Fix`

- All uppercase, spaces become underscores, no special characters
- **Never push to `main`**

**Commit prefix:** `[AI-AGENT] Fix:`

- Keep commits **under 20**

---

## API Responsibility

| Endpoint | Method | Description |
|---|---|---|
| `run-agent` | `POST` | Starts full autonomous run |
| `status` | `GET` | Progress polling for frontend |
| `results` | `GET` | Returns `results.json` |

---

## Retry Strategy

Maximum retries: **5**

Each iteration: `Execute → Detect → Fix → Push → Validate`

Stop when CI passes or retries exhausted.

---

## Performance Awareness

Speed bonus awarded if total run completes within **5 minutes**.

Favor:
- Execution-first detection
- Minimal context LLM calls
- Controlled iterations
- Parallelizable steps where safe

---

## Determinism Requirement

- No manual intervention
- No hardcoded test paths
- All fixes must be reproducible
- System must be deterministic where possible
- Output formatting is always code-generated, never LLM-generated

---

## Scoring

| Factor | Detail |
|---|---|
| Base score | 100 |
| Speed bonus | Under 5 minutes |
| Commit penalty | Exceeds 20 commits |
| Highest weight | Test case accuracy |

**Correctness is prioritized over complexity.**
