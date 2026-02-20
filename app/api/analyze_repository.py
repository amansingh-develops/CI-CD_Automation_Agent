"""
POST /api/analyze-repository
============================
Bridge endpoint that connects the React frontend to the real Orchestrator.

Accepts the frontend's AnalyzeRequest (repo_url, team_name, leader_name),
runs the full healing pipeline, then transforms the AgentState output into
the AnalyzeResponse shape expected by the Dashboard component.
"""
import time
import logging
import asyncio
from typing import List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.agents.orchestrator import Orchestrator, _GUARDRAIL_ABORT
from app.agents.fix_agent import FixAgent
from app.core.config import GITHUB_TOKEN

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Frontend API"])

# Hard timeout ceiling (seconds)
_MAX_TIMEOUT = 480  # 8 minutes absolute cap


# ---------------------------------------------------------------------------
# Request / Response schemas (matching frontend expectations)
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    repo_url: str
    team_name: str
    leader_name: str


class Fix(BaseModel):
    file: str
    type: str
    line: int
    commit: str
    status: str


class TimelineItem(BaseModel):
    iteration: str
    status: str
    timestamp: str
    time: str


class AnalyzeResponse(BaseModel):
    repo_url: str
    team_name: str
    leader_name: str
    branch_name: str
    fixes: List[Fix]
    timeline: List[TimelineItem]
    total_failures_detected: int
    total_fixes_applied: int
    total_commits: int
    total_time_seconds: int
    total_time_formatted: str
    base_score: int
    speed_bonus: int
    efficiency_penalty: int
    final_score: int


# ---------------------------------------------------------------------------
# Helper: Transform AgentState → AnalyzeResponse
# ---------------------------------------------------------------------------
def _transform_state_to_response(
    state: dict,
    request: AnalyzeRequest,
    elapsed_seconds: float,
) -> AnalyzeResponse:
    """Convert the orchestrator's AgentState dict into the frontend schema."""

    # --- Branch name ---
    branch_name = (
        f"{request.team_name.upper().replace(' ', '_')}_"
        f"{request.leader_name.upper().replace(' ', '_')}_AI_FIX"
    )

    # --- Build fixes list from snapshots ---
    fixes: List[Fix] = []
    snapshots = state.get("snapshots", [])

    for snap in snapshots:
        bug_reports = getattr(snap, "bug_reports", []) if hasattr(snap, "bug_reports") else []
        fixes_applied = getattr(snap, "fixes_applied", []) if hasattr(snap, "fixes_applied") else []

        # Add bugs as detected issues
        for bug in bug_reports:
            # BugReport is a Pydantic model with: file_path, line_number, bug_type, message
            b_file_path = getattr(bug, "file_path", "unknown")
            b_bug_type = getattr(bug, "bug_type", "UNKNOWN")
            b_line_num = getattr(bug, "line_number", 0)

            # Determine if this bug was fixed
            fix_status = "SUCCESS"
            if state.get("status") != "success":
                fix_status = "FAILED"

            fixes.append(Fix(
                file=b_file_path,
                type=b_bug_type,
                line=b_line_num,
                commit=f"[AI-AGENT] Fix {b_bug_type.lower()} issue in {b_file_path}",
                status=fix_status,
            ))

    # --- Build timeline from snapshots ---
    timeline: List[TimelineItem] = []
    total_iterations = len(snapshots)

    for idx, snap in enumerate(snapshots):
        iteration_num = getattr(snap, "iteration", idx + 1)
        ci_status = getattr(snap, "ci_status", "unknown") if hasattr(snap, "ci_status") else "unknown"
        iter_time = getattr(snap, "iteration_time_seconds", 0) if hasattr(snap, "iteration_time_seconds") else 0

        timeline_status = "PASSED" if ci_status == "success" else "FAILED"
        formatted_time = f"{int(iter_time)}s"

        # Calculate timestamp for this iteration
        now = datetime.now(timezone.utc)
        timestamp_str = now.strftime('%I:%M:%S %p')

        timeline.append(TimelineItem(
            iteration=f"{iteration_num}/{total_iterations}",
            status=timeline_status,
            timestamp=timestamp_str,
            time=formatted_time,
        ))

    # --- Counters ---
    total_failures_detected = state.get("total_bugs_found", 0)
    total_fixes_applied_count = state.get("total_fixes_applied", 0)
    total_commits = state.get("commit_count", 0)

    # If no fixes from real data, use the fixes list length
    if total_failures_detected == 0 and len(fixes) > 0:
        total_failures_detected = len(fixes)
    if total_fixes_applied_count == 0 and len(fixes) > 0:
        total_fixes_applied_count = len([f for f in fixes if f.status == "SUCCESS"])

    # --- Time ---
    total_time_seconds = int(elapsed_seconds)
    minutes = total_time_seconds // 60
    seconds = total_time_seconds % 60
    total_time_formatted = f"{minutes}m {seconds:02d}s"

    # --- Score calculation ---
    # Base score: 100 if success, scaled by fixes otherwise
    if state.get("status") == "success":
        base_score = 100
    else:
        # Partial score based on fixes applied
        if total_failures_detected > 0:
            base_score = int((total_fixes_applied_count / total_failures_detected) * 100)
        else:
            base_score = 0

    speed_bonus = 10 if total_time_seconds < 300 else 0
    efficiency_penalty = max(0, (total_commits - 20) * -2) if total_commits > 20 else 0
    final_score = max(0, base_score + speed_bonus + efficiency_penalty)

    return AnalyzeResponse(
        repo_url=request.repo_url,
        team_name=request.team_name,
        leader_name=request.leader_name,
        branch_name=branch_name,
        fixes=fixes,
        timeline=timeline,
        total_failures_detected=total_failures_detected,
        total_fixes_applied=total_fixes_applied_count,
        total_commits=total_commits,
        total_time_seconds=total_time_seconds,
        total_time_formatted=total_time_formatted,
        base_score=base_score,
        speed_bonus=speed_bonus,
        efficiency_penalty=efficiency_penalty,
        final_score=final_score,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.post("/analyze-repository", response_model=AnalyzeResponse)
async def analyze_repository(request: AnalyzeRequest):
    """
    Run the full autonomous healing pipeline against a GitHub repository.

    Clones the repo, detects failures, applies AI-generated fixes,
    pushes commits, and returns structured results for the dashboard.
    """
    # --- Token check ---
    if not GITHUB_TOKEN:
        logger.error("GITHUB_TOKEN is missing from environment/config")
        raise HTTPException(
            status_code=400,
            detail="GITHUB_TOKEN not set — cannot clone repositories. "
                   "Please set GITHUB_TOKEN in the backend .env file."
        )

    logger.info(
        f"[API] New analysis request for repo: {request.repo_url} "
        f"(Team: {request.team_name}, Leader: {request.leader_name})"
    )

    # --- Build orchestrator ---
    fix_agent = FixAgent()
    orchestrator = Orchestrator(fix_agent=fix_agent, github_token=GITHUB_TOKEN)

    # --- Run with timeout ---
    start = time.time()
    logger.info(f"[API] Starting Orchestrator for {request.repo_url}...")

    try:
        state = await asyncio.wait_for(
            orchestrator.run(
                repo_url=request.repo_url,
                branch="main",
                team_name=request.team_name,
                leader_name=request.leader_name,
            ),
            timeout=_MAX_TIMEOUT,
        )
    except asyncio.TimeoutError:
        elapsed = time.time() - start
        logger.warning(f"[API] TIMEOUT reached after {elapsed:.1f}s for {request.repo_url}")
        # Use real partial state from the orchestrator (not blank zeros)
        state = orchestrator._partial_state or {}
        state["status"] = "timeout"
        state["execution_summary"] = f"Timed out after {elapsed:.0f}s"
        state.setdefault("total_bugs_found", 0)
        state.setdefault("total_fixes_applied", 0)
        state.setdefault("commit_count", orchestrator.git_agent.commit_count)
        state.setdefault("snapshots", [])
        # Push whatever commits were made before timeout
        workspace = state.get("workspace_path", "")
        branch = state.get("branch_name", "")
        if workspace and branch and orchestrator.git_agent.commit_count > 0:
            try:
                logger.info(f"[API] Pushing {orchestrator.git_agent.commit_count} partial commit(s) before timeout response...")
                orchestrator.git_agent.push(workspace, branch)
            except Exception as push_exc:
                logger.warning(f"[API] Partial push failed: {push_exc}")
    except Exception as exc:
        logger.error(f"[API] FATAL: Orchestrator failed for {request.repo_url}: {str(exc)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Analysis failed: {str(exc)}"
        )

    elapsed = time.time() - start
    logger.info(
        f"[API] Analysis finished for {request.repo_url} in {elapsed:.1f}s. "
        f"Status: {state.get('status')} | Bugs: {state.get('total_bugs_found')} | "
        f"Fixes: {state.get('total_fixes_applied')}"
    )

    return _transform_state_to_response(state, request, elapsed)
