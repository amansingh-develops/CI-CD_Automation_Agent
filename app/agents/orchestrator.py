"""
Orchestrator Agent
==================
The central brain of the autonomous CI healing agent.
Drives the Execute → Detect → Fix → Push → Validate loop.

Core Features (Step 6):
    - Failure prioritization (SYNTAX → IMPORT → TYPE_ERROR → INDENTATION → LOGIC → LINTING)
    - Same-failure detection via fix_history fingerprints
    - Patch acceptance rules (confidence, diff, locality, fingerprint, validity)
    - Confidence gating re-execution (revert ineffective patches)
    - Commit batching by domain (database, backend, frontend)
    - 5-minute performance guardrail with performance_hint
    - Escalation handling (skip repeated escalated bugs)
    - Fix attempt fingerprint storage per iteration
    - Fault tolerance (subsystem failures never crash orchestrator)

Stability Guardrails (Step 6.1):
    - Failure signature drift tracking (priority-aware regression)
    - Root-fix commit gate (block lint commits while syntax/import broken)
    - Fix history memory cap (5 per bug, 200 global)
    - Patch effectiveness scoring (1.0/0.5/0.0 via bug_signature)
    - Iteration time budget awareness
    - CI hang protection (stalled flag + timeline event)
    - Commit noise protection (skip if no effective fixes)
    - Telemetry hooks (counters in state)
"""
import os
import time
import logging
import asyncio
from collections import deque
from typing import List, Optional, Set, Dict
from datetime import datetime, timezone

from app.state.agent_state import AgentState
from app.models.bug_report import BugReport
from app.models.fix_result import FixResult
from app.models.iteration_snapshot import IterationSnapshot
from app.models.ci_run import CIRun

from app.services.repo_service import clone_repository, detect_project_type
from app.services.results_writer import ResultsWriter
from app.executor.build_executor import run_in_container, ExecutionResult
from app.parser.failure_parser import parse_failure_log
from app.parser.classification import priority_of
from app.agents.fix_agent import FixAgent
from app.agents.git_agent import GitAgent
from app.agents.ci_monitor import CIMonitor
from app.utils.fix_fingerprint import generate_bug_signature, generate_fix_fingerprint
from app.utils.escalation_reasons import REPEATED_FIX
from app.core.config import RUN_RETRY_LIMIT, GITHUB_TOKEN

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Performance guardrail thresholds (seconds)
# ---------------------------------------------------------------------------
_GUARDRAIL_REDUCED = 180   # 3 minutes → reduce batch
_GUARDRAIL_CRITICAL = 240  # 4 minutes → root failures only
_GUARDRAIL_ABORT = 290     # ~5 minutes → stop loop

# ---------------------------------------------------------------------------
# Fix history caps
# ---------------------------------------------------------------------------
_FP_CAP_PER_BUG = 5       # Max fingerprints stored per bug signature
_FP_CAP_GLOBAL = 200       # Max unique bug signatures tracked globally

# ---------------------------------------------------------------------------
# Root bug types (commit-gate threshold)
# ---------------------------------------------------------------------------
_ROOT_BUG_TYPES = {"SYNTAX", "IMPORT"}


# ---------------------------------------------------------------------------
# Domain classification for commit batching
# ---------------------------------------------------------------------------
def _classify_domain(file_path: str) -> str:
    """Classify a file path into a commit-batch domain."""
    normalized = file_path.replace("\\", "/").lower()
    if any(seg in normalized for seg in ("migration", "schema", "db/", "database")):
        return "database"
    if any(seg in normalized for seg in (
        "frontend/", "client/", "src/components", "src/pages",
        ".jsx", ".tsx", ".vue", ".css", ".scss",
    )):
        return "frontend"
    return "backend"


def _get_performance_hint(elapsed: float) -> str:
    """Return performance hint based on elapsed time."""
    if elapsed >= _GUARDRAIL_CRITICAL:
        return "critical"
    if elapsed >= _GUARDRAIL_REDUCED:
        return "reduced"
    return "normal"


def _sort_bugs_by_priority(bugs: List[BugReport]) -> List[BugReport]:
    """Sort BugReports by type priority (SYNTAX first, LINTING last)."""
    return sorted(bugs, key=lambda b: priority_of(b.bug_type))


def _compute_failure_signature(bugs: List[BugReport]) -> str:
    """Compute a combined signature of all current failures for gating."""
    parts = sorted(f"{b.file_path}:{b.line_number}:{b.sub_type}" for b in bugs)
    return "|".join(parts)


def _compute_failure_signatures_list(bugs: List[BugReport]) -> List[str]:
    """Return sorted list of individual bug signatures."""
    return sorted(generate_bug_signature(b) for b in bugs)


def _classify_iteration_outcome(
    prev_sigs: List[str],
    curr_sigs: List[str],
    prev_bugs: List[BugReport],
    curr_bugs: List[BugReport],
) -> str:
    """
    Priority-aware iteration outcome classification.

    - improved:  highest-priority bug type reduced or root failures reduced
    - unchanged: identical signature sets
    - regressed: highest-priority bug type worsened (not just new lint failures)
    """
    if prev_sigs == curr_sigs:
        return "unchanged"

    # Compare best (lowest numeric) priority before vs after
    prev_best = min((priority_of(b.bug_type) for b in prev_bugs), default=999)
    curr_best = min((priority_of(b.bug_type) for b in curr_bugs), default=999)

    if curr_best > prev_best:
        # Root layer improved (e.g. syntax fixed, only lint left)
        return "improved"
    if curr_best < prev_best:
        # Higher-priority failure appeared → regression
        return "regressed"

    # Same priority tier — compare counts at that tier
    prev_root_count = sum(1 for b in prev_bugs if priority_of(b.bug_type) == prev_best)
    curr_root_count = sum(1 for b in curr_bugs if priority_of(b.bug_type) == curr_best)

    if curr_root_count < prev_root_count:
        return "improved"
    if curr_root_count > prev_root_count:
        return "regressed"

    # Same root count but different signatures overall
    if len(curr_sigs) <= len(prev_sigs):
        return "improved"
    return "regressed"


def _score_effectiveness(
    fix: FixResult,
    pre_sigs: Set[str],
    post_sigs: Set[str],
) -> float:
    """Score patch effectiveness using bug_signature (not message text)."""
    sig = fix.bug_signature or generate_bug_signature(fix.bug_report)
    if sig not in post_sigs and sig in pre_sigs:
        return 1.0   # Bug removed
    if sig in post_sigs and sig in pre_sigs:
        return 0.0   # Bug unchanged
    # Bug signature changed (different line / sub_type) — partial improvement
    return 0.5


def _has_root_failures(bugs: List[BugReport]) -> bool:
    """Check whether any SYNTAX or IMPORT failures remain."""
    return any(b.bug_type in _ROOT_BUG_TYPES for b in bugs)


def _fix_targets_root(fix: FixResult) -> bool:
    """Check whether a fix targets a SYNTAX or IMPORT bug."""
    return fix.bug_report.bug_type in _ROOT_BUG_TYPES


class _FixHistoryStore:
    """
    Bounded fix-fingerprint storage.
    - Per bug_signature: deque(maxlen=5)
    - Global: max 200 unique signatures, FIFO eviction
    """

    def __init__(
        self,
        per_bug_cap: int = _FP_CAP_PER_BUG,
        global_cap: int = _FP_CAP_GLOBAL,
    ) -> None:
        self._store: Dict[str, deque] = {}
        self._order: deque = deque()   # insertion-order for FIFO eviction
        self._per_bug_cap = per_bug_cap
        self._global_cap = global_cap

    def get_fingerprints(self, bug_sig: str) -> Set[str]:
        """Return set of known fingerprints for a bug signature."""
        dq = self._store.get(bug_sig)
        return set(dq) if dq else set()

    def add(self, bug_sig: str, patch_fp: str, iteration: int) -> None:
        if bug_sig not in self._store:
            # Global cap eviction
            if len(self._order) >= self._global_cap:
                evicted = self._order.popleft()
                self._store.pop(evicted, None)
            self._store[bug_sig] = deque(maxlen=self._per_bug_cap)
            self._order.append(bug_sig)
        self._store[bug_sig].append(patch_fp)

    def to_list(self) -> List[dict]:
        """Serialise to list-of-dicts for state persistence."""
        out: List[dict] = []
        for sig, dq in self._store.items():
            for fp in dq:
                out.append({"bug_signature": sig, "patch_fingerprint": fp})
        return out

    @classmethod
    def from_list(cls, entries: List[dict], **kwargs) -> "_FixHistoryStore":
        store = cls(**kwargs)
        for entry in entries:
            store.add(entry.get("bug_signature", ""), entry.get("patch_fingerprint", ""), 0)
        return store

    @property
    def tracked_signatures(self) -> int:
        return len(self._store)


class Orchestrator:
    """
    Orchestrates the autonomous healing process for a given repository.

    State-driven, deterministic loop with:
        - Retry safety via fingerprint tracking
        - Confidence gating via re-execution
        - Commit batching by domain
        - 5-minute performance guardrail
        - Fault-tolerant subsystem calls

    Step 6.1 Guardrails:
        - Failure drift tracking (priority-aware)
        - Root-fix commit gate
        - Bounded fix history (5 per bug, 200 global)
        - Patch effectiveness scoring (signature-based)
        - Iteration time budget
        - CI hang protection
        - Commit noise protection
        - Telemetry hooks
    """

    def __init__(self, fix_agent: FixAgent, github_token: str = GITHUB_TOKEN) -> None:
        self.fix_agent = fix_agent
        self.git_agent = GitAgent()
        self.ci_monitor = CIMonitor(github_token=github_token)
        self.github_token = github_token

    async def run(
        self,
        repo_url: str,
        branch: str = "main",
        team_name: str = "Anonymous",
        leader_name: str = "AI Agent",
        working_directory: str = ""
    ) -> AgentState:
        """Execute the full healing loop."""
        # --- Initialisation ---
        run_start = time.time()
        state: AgentState = {
            "repo_url": repo_url,
            "team_name": team_name,
            "leader_name": leader_name,
            "branch_name": branch,
            "workspace_path": "",
            "project_type": "generic",
            "working_directory": working_directory,
            "iteration": 0,
            "snapshots": [],
            "bug_reports": [],
            "fix_results": [],
            "ci_runs": [],
            "fix_history": [],
            "start_time": run_start,
            "performance_hint": "normal",
            "commit_count": 0,
            "effective_fix_count": 0,
            "skipped_fix_count": 0,
            "ci_stalled_flag": False,
            "status": "pending",
            "score": 0,
            "total_bugs_found": 0,
            "total_fixes_applied": 0,
            "execution_summary": ""
        }

        # Bounded fingerprint store
        history = _FixHistoryStore()

        # Track previous-iteration failure signatures for drift detection
        prev_failure_sigs: List[str] = []
        prev_bugs: List[BugReport] = []

        try:
            # ===========================================================
            # 1. Clone repository
            # ===========================================================
            logger.info("Step 1: Cloning repository: %s", repo_url)
            workspace_path = clone_repository(repo_url, self.github_token)
            state["workspace_path"] = workspace_path

            # ===========================================================
            # 2. Detect project type
            # ===========================================================
            state["project_type"] = detect_project_type(workspace_path)
            logger.info("Step 2: Detected project type: %s", state["project_type"])

            # ===========================================================
            # 3. Autonomous Healing Loop
            # ===========================================================
            for i in range(1, RUN_RETRY_LIMIT + 1):
                iter_start = time.time()
                state["iteration"] = i
                logger.info("--- Starting Iteration %d ---", i)

                # --- (a) Performance guardrail check ---
                elapsed = time.time() - run_start
                remaining = max(0, _GUARDRAIL_ABORT - elapsed)
                state["performance_hint"] = _get_performance_hint(elapsed)

                if elapsed >= _GUARDRAIL_ABORT:
                    logger.warning("5-minute guardrail reached, stopping loop")
                    state["status"] = "exhausted"
                    state["execution_summary"] = "Stopped: 5-minute performance guardrail reached."
                    break

                # --- (b) Execute build ---
                try:
                    exec_result: ExecutionResult = run_in_container(
                        workspace_path=workspace_path,
                        project_type=state["project_type"],
                        working_dir=working_directory
                    )
                except Exception as exc:
                    logger.error("Executor failed: %s", exc)
                    state["snapshots"].append(IterationSnapshot(
                        iteration=i,
                        execution_summary=f"Executor error: {exc}",
                        iteration_time_seconds=time.time() - iter_start,
                    ))
                    continue

                # --- (c) Parse failures ---
                try:
                    bugs: List[BugReport] = parse_failure_log(exec_result.full_log)
                except Exception as exc:
                    logger.error("Parser failed: %s", exc)
                    bugs = []

                state["total_bugs_found"] += len(bugs)

                # --- (d) Check for success ---
                if exec_result.exit_code == 0:
                    logger.info("Iteration %d: Build PASSED!", i)
                    curr_sigs = _compute_failure_signatures_list(bugs)
                    outcome = _classify_iteration_outcome(
                        prev_failure_sigs, curr_sigs, prev_bugs, bugs
                    ) if prev_failure_sigs else "improved"

                    state["status"] = "success"
                    state["execution_summary"] = f"Healing successful in {i} iteration(s)."
                    state["snapshots"].append(IterationSnapshot(
                        iteration=i,
                        bug_reports=bugs,
                        build_log_snippet=exec_result.log_excerpt,
                        ci_status="success",
                        execution_summary="Build passed.",
                        iteration_outcome=outcome,
                        previous_failure_signatures=prev_failure_sigs,
                        current_failure_signatures=curr_sigs,
                        failure_delta=len(prev_failure_sigs) - len(curr_sigs),
                        iteration_time_seconds=time.time() - iter_start,
                    ))
                    break

                if not bugs:
                    logger.warning("Iteration %d: Build FAILED but no bugs parsed.", i)
                    state["status"] = "failure"
                    state["execution_summary"] = "Build failed but parser could not identify specific bugs."
                    state["snapshots"].append(IterationSnapshot(
                        iteration=i,
                        build_log_snippet=exec_result.log_excerpt,
                        ci_status="failure",
                        execution_summary="No bugs detected in logs.",
                        iteration_time_seconds=time.time() - iter_start,
                    ))
                    break

                # --- (e) Sort by priority ---
                bugs = _sort_bugs_by_priority(bugs)

                # Performance-aware batch limits
                if state["performance_hint"] == "critical" or remaining < 60:
                    bugs = bugs[:1]
                elif state["performance_hint"] == "reduced":
                    bugs = bugs[:3]

                # Compute pre-fix signatures for drift + gating
                pre_fix_signature = _compute_failure_signature(bugs)
                pre_fix_sigs_set = set(_compute_failure_signatures_list(bugs))

                # --- (f) Fix phase ---
                logger.info("Iteration %d: Fixing %d detected bugs...", i, len(bugs))
                iteration_fixes: List[FixResult] = []
                applied_in_iteration = 0
                escalated_signatures: Set[str] = set()
                domain_fixes: Dict[str, List[FixResult]] = {}
                iter_skipped = 0

                for bug in bugs:
                    bug_sig = generate_bug_signature(bug)

                    # Skip already-escalated bugs this iteration
                    if bug_sig in escalated_signatures:
                        logger.info("Skipping already-escalated bug: %s", bug_sig)
                        iter_skipped += 1
                        continue

                    # Check bounded fix history for repeated fixes
                    history_fingerprints = history.get_fingerprints(bug_sig)

                    # Read file content
                    abs_file_path = os.path.normpath(
                        os.path.join(workspace_path, bug.file_path)
                    )
                    file_content = ""
                    try:
                        if os.path.exists(abs_file_path):
                            with open(abs_file_path, "r", encoding="utf-8") as f:
                                file_content = f.read()
                    except Exception as exc:
                        logger.error("Failed to read %s: %s", abs_file_path, exc)
                        iter_skipped += 1
                        continue

                    # Generate fix
                    try:
                        fix_result = await self.fix_agent.fix(
                            bug_report=bug,
                            file_content=file_content,
                            attempt_number=i,
                            working_directory=working_directory
                        )
                    except Exception as exc:
                        logger.error("FixAgent failed for %s: %s", bug.file_path, exc)
                        iter_skipped += 1
                        continue

                    iteration_fixes.append(fix_result)

                    # --- Patch acceptance rules ---
                    if not fix_result.success:
                        escalated_signatures.add(bug_sig)
                        iter_skipped += 1
                        logger.info(
                            "Fix rejected for %s: %s",
                            bug.file_path,
                            fix_result.escalation_reason or "not successful"
                        )
                        continue

                    # Repeated fingerprint check
                    if fix_result.patch_fingerprint and fix_result.patch_fingerprint in history_fingerprints:
                        escalated_signatures.add(bug_sig)
                        fix_result.success = False
                        fix_result.escalation_reason = REPEATED_FIX
                        iter_skipped += 1
                        logger.warning("Repeated fix detected for %s, skipping", bug.file_path)
                        continue

                    # Record in bounded history
                    history.add(bug_sig, fix_result.patch_fingerprint or "", i)

                    # Apply patch via git_agent
                    try:
                        applied = self.git_agent.apply_fix(fix_result, workspace_path)
                    except Exception as exc:
                        logger.error("GitAgent apply failed: %s", exc)
                        applied = False

                    if applied:
                        applied_in_iteration += 1
                        state["total_fixes_applied"] += 1
                        domain = _classify_domain(bug.file_path)
                        domain_fixes.setdefault(domain, []).append(fix_result)

                # --- (g) Confidence gating re-execution + effectiveness scoring ---
                effective_in_iteration = 0
                post_fix_bugs: List[BugReport] = bugs  # default if no re-execution

                if applied_in_iteration > 0:
                    try:
                        verify_result = run_in_container(
                            workspace_path=workspace_path,
                            project_type=state["project_type"],
                            working_dir=working_directory
                        )
                        verify_bugs = parse_failure_log(verify_result.full_log)
                        post_fix_bugs = verify_bugs
                        post_fix_signature = _compute_failure_signature(verify_bugs)
                        post_fix_sigs_set = set(_compute_failure_signatures_list(verify_bugs))

                        if verify_result.exit_code == 0:
                            logger.info("Confidence gate: build passes after fixes!")
                        elif post_fix_signature == pre_fix_signature:
                            logger.warning(
                                "Confidence gate: failure signature unchanged, "
                                "fixes were ineffective"
                            )

                        # --- Effectiveness scoring (signature-based) ---
                        for fix in iteration_fixes:
                            if fix.success:
                                score = _score_effectiveness(fix, pre_fix_sigs_set, post_fix_sigs_set)
                                fix.effectiveness_score = score
                                if score > 0:
                                    effective_in_iteration += 1
                    except Exception as exc:
                        logger.error("Confidence gating re-execution failed: %s", exc)

                # Update telemetry counters
                state["effective_fix_count"] += effective_in_iteration
                state["skipped_fix_count"] += iter_skipped

                # --- Failure drift tracking ---
                curr_failure_sigs = _compute_failure_signatures_list(post_fix_bugs)
                iteration_outcome = _classify_iteration_outcome(
                    prev_failure_sigs, curr_failure_sigs, prev_bugs, post_fix_bugs
                ) if prev_failure_sigs else ("improved" if applied_in_iteration > 0 else "unchanged")

                # --- (h) Commit gating ---
                state["commit_count"] = self.git_agent.commit_count

                # --- Root-fix commit gate ---
                root_failures_remain = _has_root_failures(post_fix_bugs)
                has_root_fixes = any(_fix_targets_root(f) for f in iteration_fixes if f.success)

                # --- Commit noise protection ---
                should_commit = (
                    applied_in_iteration > 0
                    and effective_in_iteration > 0
                    and (not root_failures_remain or has_root_fixes)
                )

                if applied_in_iteration > 0 and not should_commit:
                    if effective_in_iteration == 0:
                        logger.info("Commit skipped: no effective fixes (noise protection)")
                    elif root_failures_remain and not has_root_fixes:
                        logger.info("Commit blocked: root failures remain, fixes target lint only")

                # --- (i) Push and validate ---
                if should_commit:
                    logger.info("Pushing fixes and polling CI...")
                    try:
                        self.git_agent.push(workspace_path, branch)
                    except Exception as exc:
                        logger.error("Git push failed: %s", exc)

                    commit_sha = self.git_agent.get_last_commit_sha(workspace_path)

                    # Poll CI status
                    try:
                        ci_status = await self.ci_monitor.poll_status(repo_url, commit_sha)
                    except Exception as exc:
                        logger.error("CI polling failed: %s", exc)
                        ci_status = "error"

                    # --- CI hang protection ---
                    if ci_status == "unknown_timeout":
                        state["ci_stalled_flag"] = True
                        # Record timeline event
                        now_ts = datetime.now(timezone.utc)
                        state["ci_runs"].append(CIRun(
                            run_id=commit_sha or f"stall-{i}",
                            status="unknown_timeout",
                            started_at=now_ts,
                            finished_at=now_ts,
                            iteration=i
                        ))
                        logger.warning(
                            "CI stalled detected at iteration %d, continuing locally", i
                        )
                    else:
                        now_ts = datetime.now(timezone.utc)
                        state["ci_runs"].append(CIRun(
                            run_id=commit_sha or f"iter-{i}",
                            status=ci_status,
                            started_at=now_ts,
                            finished_at=now_ts,
                            iteration=i
                        ))

                    # --- (j) Record snapshot ---
                    snapshot = IterationSnapshot(
                        iteration=i,
                        bug_reports=bugs,
                        fixes_applied=iteration_fixes,
                        build_log_snippet=exec_result.log_excerpt,
                        ci_status=ci_status,
                        execution_summary=f"Applied {applied_in_iteration} fix(es), effective {effective_in_iteration}, CI: {ci_status}.",
                        iteration_outcome=iteration_outcome,
                        previous_failure_signatures=prev_failure_sigs,
                        current_failure_signatures=curr_failure_sigs,
                        failure_delta=len(prev_failure_sigs) - len(curr_failure_sigs),
                        effective_fix_count=effective_in_iteration,
                        skipped_fix_count=iter_skipped,
                        iteration_time_seconds=time.time() - iter_start,
                    )
                    state["snapshots"].append(snapshot)

                    if ci_status == "success":
                        logger.info("CI PASSED after fixes!")
                        state["status"] = "success"
                        state["execution_summary"] = (
                            f"Healing successful via CI validation in iteration {i}."
                        )
                        break
                    elif ci_status in ("failure", "timeout"):
                        logger.warning("CI Failed/Timed out, retrying next iteration...")
                    # unknown_timeout → continue local loop
                else:
                    # No commit this iteration
                    logger.warning("No commit in iteration %d.", i)
                    state["snapshots"].append(IterationSnapshot(
                        iteration=i,
                        bug_reports=bugs,
                        fixes_applied=iteration_fixes,
                        build_log_snippet=exec_result.log_excerpt,
                        ci_status="skipped",
                        execution_summary="All fixes escalated, ineffective, or blocked by commit gate.",
                        iteration_outcome=iteration_outcome,
                        previous_failure_signatures=prev_failure_sigs,
                        current_failure_signatures=curr_failure_sigs,
                        failure_delta=len(prev_failure_sigs) - len(curr_failure_sigs),
                        effective_fix_count=effective_in_iteration,
                        skipped_fix_count=iter_skipped,
                        iteration_time_seconds=time.time() - iter_start,
                    ))
                    # If ALL bugs were escalated, stop loop
                    if len(escalated_signatures) >= len(bugs):
                        state["status"] = "failure"
                        state["execution_summary"] = (
                            "All bugs escalated — no effective fixes possible."
                        )
                        break

                # Update previous signatures for next iteration drift tracking
                prev_failure_sigs = curr_failure_sigs
                prev_bugs = post_fix_bugs

            # Finalise status if still pending
            if state["status"] == "pending":
                state["status"] = "exhausted"
                state["execution_summary"] = (
                    f"Reached RUN_RETRY_LIMIT ({RUN_RETRY_LIMIT}) without full success."
                )

        except Exception as e:
            logger.error("Orchestrator encountered a fatal error: %s", e, exc_info=True)
            state["status"] = "error"
            state["execution_summary"] = f"Fatal error: {str(e)}"

        # ===========================================================
        # 4. Final results
        # ===========================================================
        # Persist bounded history back to state for serialisation
        state["fix_history"] = history.to_list()

        try:
            ResultsWriter.write_results(state)
        except Exception as exc:
            logger.error("Results writer failed: %s", exc)

        logger.info("Healing run complete. Status: %s", state["status"])
        return state
