"""
Agent State
TypedDict or Dataclass defining the shared state used by LangGraph agents.
Fields: repo_url, team_name, leader_name, iteration, bug_reports, etc.
"""
from typing import TypedDict, List, Optional
from app.models.bug_report import BugReport
from app.models.fix_result import FixResult
from app.models.ci_run import CIRun
from app.models.iteration_snapshot import IterationSnapshot


class AgentState(TypedDict):
    # Repo info
    repo_url: str
    team_name: str
    leader_name: str
    branch_name: str
    workspace_path: str         # Absolute path on host
    project_type: str           # node, python, etc.
    working_directory: str      # CI-config scope

    # Progress tracking
    iteration: int
    snapshots: List[IterationSnapshot]
    bug_reports: List[BugReport]
    fix_results: List[FixResult]
    ci_runs: List[CIRun]

    # Fingerprint history — list of {bug_signature, patch_fingerprint, iteration}
    fix_history: List[dict]

    # Timing / guardrail
    start_time: float           # time.time() at start
    performance_hint: str       # "normal" / "reduced" / "critical"

    # Commit tracking
    commit_count: int

    # Telemetry (Step 6.1)
    effective_fix_count: int    # Cumulative fixes with effectiveness > 0
    skipped_fix_count: int      # Cumulative skipped/escalated fixes
    ci_stalled_flag: bool       # True if CI was detected as stalled

    # Final summary
    status: str                 # success, failure, exhausted, error
    score: int
    total_bugs_found: int
    total_fixes_applied: int
    execution_summary: str
