# EXECUTION CONTRACT — Inter-Layer Communication Agreement

This document defines the typed contracts between the three core processing layers:
**Executor**, **Parser**, and **Fix Agent**.

All downstream modules must respect these interfaces.
No layer may exceed its documented responsibilities.

---

## Executor Contract

### Input
| Field                   | Type              | Description                                 |
|-------------------------|-------------------|---------------------------------------------|
| `workspace_path`        | `str`             | Absolute path to workspace on host          |
| `project_type`          | `str \| None`     | Detected project type (optional override)   |
| `resolved_build_command`| `str`             | Shell command(s) to run inside container    |
| `timeout_seconds`       | `int`             | Max execution time before kill (default 300)|

### Output
| Field                    | Type              | Description                                |
|--------------------------|-------------------|--------------------------------------------|
| `exit_code`              | `int`             | Process exit code (0 = success)            |
| `build_log`              | `str`             | Full raw log (stdout + stderr combined)    |
| `log_excerpt`            | `str`             | First + last sections for quick display    |
| `execution_time_seconds` | `float`           | Wall clock duration                        |
| `environment_metadata`   | `dict`            | Runtime version, detected tools, image     |

### Boundary Rules
- Executor **observes execution only**
- Executor **never fixes code**
- Executor **never commits changes**
- Executor **never interprets errors** — that is the Parser's job

---

## Parser Contract

### Input
| Field            | Type   | Description                                 |
|------------------|--------|---------------------------------------------|
| `build_log`      | `str`  | Full raw log string from Executor           |
| `workspace_path` | `str`  | Repo root for path normalisation            |

### Output
| Field              | Type          | Description                               |
|--------------------|---------------|-------------------------------------------|
| `file_path`        | `str`         | Repo-relative path, forward slashes       |
| `line_number`      | `int`         | Line number (>= 1)                        |
| `error_type`       | `str`         | One of six allowed bug types              |
| `message`          | `str`         | Raw error message for context             |
| `test_name`        | `str \| None` | Failing test name (if applicable)         |
| `confidence_score` | `float \| None` | Parser confidence in extraction (0–1)   |

Returns: `List[BugReport]`

### Boundary Rules
- Parser **must be deterministic**: same log → same BugReports, always
- **No LLM** is allowed in this layer
- Parser uses regex and heuristic matching only

---

## Fix Agent Contract

### Input
| Field                    | Type             | Description                               |
|--------------------------|------------------|-------------------------------------------|
| `bug_report`             | `BugReport`      | Structured error from Parser              |
| `file_snippet`           | `str`            | Source code ±3 lines around the error     |
| `error_message`          | `str`            | Related raw error message                 |
| `previous_attempt_info`  | `dict \| None`   | Info from prior failed fix attempt        |

### Output
| Field              | Type    | Description                                  |
|--------------------|---------|----------------------------------------------|
| `patched_content`  | `str`   | Full patched file content                    |
| `fix_reason`       | `str`   | Brief explanation of the applied fix         |
| `confidence_score` | `float` | LLM self-assessed confidence (0–1)          |

### Boundary Rules
- Fix Agent **must produce minimal diff** (fix only reported lines ±3)
- Fix Agent **does not format the output string** — only `output_formatter` does
- Fix Agent **does not commit** — only Git Agent commits
- Fix Agent **preserves comments** and does not refactor unrelated code
- Confidence gating: fixes below threshold are rejected before commit

---

## Data Flow Summary

```
Orchestrator
    │
    ├─► Executor  ──► raw build_log + exit_code
    │                       │
    │                       ▼
    ├─► Parser    ──► List[BugReport]
    │                       │
    │                       ▼
    ├─► Fix Agent ──► patched_content + confidence
    │                       │
    │                       ▼
    ├─► Git Agent ──► commit + push
    │                       │
    │                       ▼
    └─► CI Monitor ──► pass/fail verdict
```

Each layer communicates through typed objects only.
No layer bypasses the Orchestrator.
