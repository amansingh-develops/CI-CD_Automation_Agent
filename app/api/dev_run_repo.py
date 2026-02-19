"""
DEV: Run Repository Endpoint
=============================
Development-only diagnostic endpoint for running the full orchestrator
against a real GitHub repository.

Route: POST /dev/run-repo

Safety:
    - Disabled by default (requires ENABLE_DEV_ENDPOINT=true)
    - Hard timeout ceiling (480s / 8 minutes)
    - Rejects non-GitHub URLs
    - Does not expose patch contents or secrets

This endpoint is diagnostic only — it does not affect production
API contracts, scoring logic, or dashboard schemas.
"""
import os
import re
import uuid
import time
import logging
import asyncio
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.agents.orchestrator import Orchestrator, _GUARDRAIL_ABORT
from app.agents.fix_agent import FixAgent
from app.core.config import GITHUB_TOKEN, RUN_RETRY_LIMIT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dev", tags=["Dev"])

# ---------------------------------------------------------------------------
# Environment gate
# ---------------------------------------------------------------------------
_DEV_ENABLED = os.getenv("ENABLE_DEV_ENDPOINT", "false").lower() == "true"

# Hard timeout ceiling (seconds)
_MAX_TIMEOUT = 480  # 8 minutes absolute cap

# GitHub URL pattern
_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/[\w.\-]+/[\w.\-]+(\.git)?/?$"
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------
class DevRunRequest(BaseModel):
    repo_url: str
    team_name: str = "DevTest"
    leader_name: str = "Dev Runner"
    max_iterations: Optional[int] = None       # Override RUN_RETRY_LIMIT
    timeout_override: Optional[int] = None     # Seconds, capped at _MAX_TIMEOUT

    @field_validator("repo_url")
    @classmethod
    def validate_github_url(cls, v: str) -> str:
        if not _GITHUB_URL_RE.match(v.strip()):
            raise ValueError("Only public GitHub repository URLs are accepted")
        return v.strip()


class IterationOutcomeSummary(BaseModel):
    iteration: int
    outcome: str           # improved / unchanged / regressed / ""
    bug_count: int
    effective_fixes: int
    skipped_fixes: int
    ci_status: str


class DevRunResponse(BaseModel):
    run_id: str
    detected_project_type: str
    iteration_count: int
    final_status: str      # success / failure / exhausted / timeout / error
    total_bug_reports_detected: int
    total_fixes_applied: int
    commit_count: int
    ci_polling_events_count: int
    performance_mode_last_iteration: str
    log_excerpt: str
    # Observability
    iteration_outcomes: List[IterationOutcomeSummary]
    stalled_ci_detected: bool
    repeated_fix_detected: bool
    regression_detected: bool


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.post("/run-repo", response_model=DevRunResponse)
async def dev_run_repo(request: DevRunRequest):
    """
    Run the full orchestrator against a real GitHub repository.

    Diagnostic only — returns a lightweight summary, not full results.
    Disabled unless ENABLE_DEV_ENDPOINT=true.
    """
    # --- Gate check ---
    if not _DEV_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    # --- Token check for private repos ---
    if not GITHUB_TOKEN:
        raise HTTPException(
            status_code=400,
            detail="GITHUB_TOKEN not set — cannot clone repositories"
        )

    # --- Resolve overrides ---
    run_id = str(uuid.uuid4())[:12]
    max_iter = min(request.max_iterations or RUN_RETRY_LIMIT, RUN_RETRY_LIMIT)
    timeout = min(request.timeout_override or int(_GUARDRAIL_ABORT), _MAX_TIMEOUT)

    logger.info(
        "[DEV:%s] Starting run — repo=%s max_iter=%d timeout=%ds",
        run_id, request.repo_url, max_iter, timeout
    )

    # --- Build orchestrator ---
    fix_agent = FixAgent()
    orchestrator = Orchestrator(fix_agent=fix_agent, github_token=GITHUB_TOKEN)

    # --- Run with timeout ---
    start = time.time()
    timed_out = False

    try:
        state = await asyncio.wait_for(
            orchestrator.run(
                repo_url=request.repo_url,
                branch="main",
                team_name=request.team_name,
                leader_name=request.leader_name,
            ),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        timed_out = True
        elapsed = time.time() - start
        logger.warning("[DEV:%s] Hard timeout reached after %.1fs", run_id, elapsed)
        # Build a minimal state for partial response
        state = {
            "project_type": "unknown",
            "iteration": 0,
            "status": "timeout",
            "total_bugs_found": 0,
            "total_fixes_applied": 0,
            "commit_count": 0,
            "ci_runs": [],
            "performance_hint": "critical",
            "snapshots": [],
            "ci_stalled_flag": False,
            "fix_history": [],
            "execution_summary": f"Hard timeout after {elapsed:.0f}s",
        }
    except Exception as exc:
        logger.error("[DEV:%s] Orchestrator error: %s", run_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Orchestrator error: {str(exc)}")

    elapsed_total = time.time() - start
    logger.info("[DEV:%s] Run finished in %.1fs — status=%s", run_id, elapsed_total, state["status"])

    # --- Build response ---
    snapshots = state.get("snapshots", [])

    # Iteration outcomes
    iteration_outcomes: List[IterationOutcomeSummary] = []
    for snap in snapshots:
        iteration_outcomes.append(IterationOutcomeSummary(
            iteration=snap.iteration,
            outcome=getattr(snap, "iteration_outcome", "") or "",
            bug_count=len(snap.bug_reports),
            effective_fixes=getattr(snap, "effective_fix_count", 0),
            skipped_fixes=getattr(snap, "skipped_fix_count", 0),
            ci_status=snap.ci_status,
        ))

    # Last iteration log excerpt (truncated)
    log_excerpt = ""
    if snapshots:
        raw = snapshots[-1].build_log_snippet or ""
        log_excerpt = raw[:500]

    # Observability flags
    stalled = state.get("ci_stalled_flag", False)
    repeated = any(
        entry.get("patch_fingerprint") for entry in state.get("fix_history", [])
    )  # Approximation: if history exists, repeated detection was active
    regression = any(
        getattr(s, "iteration_outcome", "") == "regressed" for s in snapshots
    )

    return DevRunResponse(
        run_id=run_id,
        detected_project_type=state.get("project_type", "unknown"),
        iteration_count=state.get("iteration", 0),
        final_status="timeout" if timed_out else state.get("status", "unknown"),
        total_bug_reports_detected=state.get("total_bugs_found", 0),
        total_fixes_applied=state.get("total_fixes_applied", 0),
        commit_count=state.get("commit_count", 0),
        ci_polling_events_count=len(state.get("ci_runs", [])),
        performance_mode_last_iteration=state.get("performance_hint", "normal"),
        log_excerpt=log_excerpt,
        iteration_outcomes=iteration_outcomes,
        stalled_ci_detected=stalled,
        repeated_fix_detected=repeated,
        regression_detected=regression,
    )
