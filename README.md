# Autonomous CI/CD Healing Agent (Backend)

## Project Overview

An intelligent backend system that automatically detects, fixes, and verifies CI/CD failures in GitHub repositories. The agent clones a repo, executes builds inside a Docker sandbox, extracts failures from runtime logs, generates minimal LLM-powered fixes, and iterates until CI passes or retries are exhausted.

## Execution-Driven Architecture

The core philosophy is **execution-first**: errors are discovered by running the project, not by statically analysing the repository. Static analysis may assist but never drives the workflow.

**Primary loop:** `Clone → Build → Detect → Fix → Commit → Push → Monitor CI → Repeat`

## Execution Contract Overview

The system enforces strict separation between its processing layers:

| Layer          | Responsibility          | Does NOT                          |
|----------------|-------------------------|-----------------------------------|
| **Executor**   | Runs builds in sandbox  | Fix code, commit, interpret errors |
| **Parser**     | Normalises logs → BugReports | Use LLM, generate output strings |
| **Fix Agent**  | Generates minimal diffs | Format output strings, commit     |
| **Git Agent**  | Branch, commit, push    | Fix code, run builds              |
| **Orchestrator** | Controls retry loop   | Directly fix or execute           |

Full typed contracts: [`EXECUTION_CONTRACT.md`](app/executor/EXECUTION_CONTRACT.md)

## Workspace Reuse Strategy

- Repository cloned **once** into `workspace/<repo-name>/`
- Workspace **reused** across all iterations
- Workspace **mounted** into Docker containers as a volume
- Never re-cloned per iteration; never cloned inside containers

## Docker Sandbox

- Ephemeral containers created per iteration
- Multi-runtime base image (Node, Python, Java, Go, Rust)
- Workspace mounted at `/workspace`
- Container destroyed after each execution
- See [`docker/Dockerfile.sandbox`](docker/Dockerfile.sandbox)

## LLM Minimal Fix Philosophy

- **Adaptive context**: start small (±3 lines), escalate on failure
- **Provider fallback**: Gemini primary → Groq fallback
- **Confidence gating**: fixes below threshold rejected before commit
- **Minimal diff**: only fix reported lines, never refactor

## Folder Structure

| Directory       | Purpose                                        |
|-----------------|------------------------------------------------|
| `app/api`       | FastAPI endpoints (run-agent, status, results) |
| `app/agents`    | Orchestrator, fix, git, CI monitor agents      |
| `app/executor`  | Docker build executor (heart of system)        |
| `app/parser`    | Runtime log → BugReport extractor              |
| `app/core`      | Constants, config, output formatter            |
| `app/services`  | Repo cloning, caching, results writing         |
| `app/models`    | Typed Pydantic objects                         |
| `app/state`     | Shared agent state (LangGraph TypedDict)       |
| `app/llm`       | Provider abstraction + routing                 |
| `app/utils`     | Path helpers, ignore rules                     |
| `app/skills`    | Domain-specific fix prompts                    |
| `docker/`       | Dockerfile.sandbox, docker-compose             |
| `workspace/`    | Cloned repos (mounted into containers)         |
| `tests/`        | Pytest test suite                              |
| `scripts/`      | Bootstrap, run, and build scripts              |

## Setup

1. `pip install -r requirements.txt`
2. Create `.env` from `.env.example`
3. Build sandbox: `./scripts/build_sandbox.sh`

## Run

```bash
python main.py
# or
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```
