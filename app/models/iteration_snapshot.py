"""
Iteration Snapshot Model
========================
Pydantic model representing one complete cycle of the autonomous healing loop.

Represents one loop cycle: Execute → Detect → Fix → Push → Validate.

Fields:
    iteration       — loop counter (1-based)
    bug_reports     — List[BugReport] found during this iteration
    fixes_applied   — List[FixResult] attempted during this iteration
    build_log_snippet — abbreviated execution log for dashboard display
    ci_status       — "pending" | "success" | "failure" | "skipped" | "unknown_timeout"
    execution_summary — brief text summarising what happened

Stability Fields (Step 6.1):
    iteration_outcome       — "improved" | "unchanged" | "regressed"
    previous_failure_signatures — sorted bug signatures from start of iteration
    current_failure_signatures  — sorted bug signatures after fixes
    failure_delta           — len(previous) - len(current), positive = improved
    effective_fix_count     — fixes with effectiveness_score > 0
    skipped_fix_count       — fixes that were skipped/escalated
    iteration_time_seconds  — wall clock time for this iteration

Used by:
    - Orchestrator to track progress across iterations
    - Dashboard timeline to display per-iteration history
    - Results writer to compile the final results.json
"""
from pydantic import BaseModel
from typing import List, Optional
from .bug_report import BugReport
from .fix_result import FixResult


class IterationSnapshot(BaseModel):
    iteration: int
    bug_reports: List[BugReport] = []
    fixes_applied: List[FixResult] = []
    build_log_snippet: str = ""
    ci_status: str = "pending"
    execution_summary: str = ""

    # --- Stability fields (Step 6.1) ---
    iteration_outcome: str = ""             # improved / unchanged / regressed
    previous_failure_signatures: List[str] = []
    current_failure_signatures: List[str] = []
    failure_delta: int = 0                  # positive = improved
    effective_fix_count: int = 0
    skipped_fix_count: int = 0
    iteration_time_seconds: float = 0.0
