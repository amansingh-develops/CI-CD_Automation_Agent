"""
Orchestrator Stability Tests (Step 6.1)
========================================
Tests for production guardrails: drift tracking, commit gating,
fingerprint cap, CI hang protection, effectiveness scoring.
"""
import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

from app.agents.orchestrator import (
    Orchestrator,
    _FixHistoryStore,
    _classify_iteration_outcome,
    _score_effectiveness,
    _has_root_failures,
)
from app.agents.fix_agent import FixAgent
from app.models.bug_report import BugReport
from app.models.fix_result import FixResult
from app.executor.build_executor import ExecutionResult
from app.utils.fix_fingerprint import generate_bug_signature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_bug(bug_type="SYNTAX", sub_type="invalid_syntax", file_path="main.py",
              line_number=10, domain="backend_python"):
    return BugReport(
        bug_type=bug_type, sub_type=sub_type,
        file_path=file_path, line_number=line_number, domain=domain
    )


def _make_fix(bug, success=True, patched_content="fixed", confidence=0.9,
              patch_fingerprint="fp1", escalation_reason="", effectiveness_score=-1.0):
    return FixResult(
        bug_report=bug, success=success, patched_content=patched_content,
        confidence=confidence, patch_fingerprint=patch_fingerprint,
        escalation_reason=escalation_reason,
        effectiveness_score=effectiveness_score,
        bug_signature=generate_bug_signature(bug),
    )


def _fail_exec(log="FAIL"):
    return ExecutionResult(exit_code=1, full_log=log, log_excerpt=log)


def _pass_exec(log="PASS"):
    return ExecutionResult(exit_code=0, full_log=log, log_excerpt=log)


@pytest.fixture
def mock_fix_agent():
    agent = MagicMock(spec=FixAgent)
    agent.fix = AsyncMock()
    return agent


@pytest.fixture
def orchestrator(mock_fix_agent):
    return Orchestrator(fix_agent=mock_fix_agent, github_token="fake-token")


def _base_patches():
    return {
        "clone": patch("app.agents.orchestrator.clone_repository", return_value="/fake/workspace"),
        "detect": patch("app.agents.orchestrator.detect_project_type", return_value="python"),
        "writer": patch("app.agents.orchestrator.ResultsWriter.write_results"),
        "exists": patch("os.path.exists", return_value=True),
        "open_file": patch("builtins.open", mock_open(read_data="def hello(): pass")),
    }


# ===================================================================
# Test 1: Repeated ineffective fixes → no commit (noise protection)
# ===================================================================
def test_repeated_ineffective_fixes_no_commit(orchestrator, mock_fix_agent):
    """When fixes have effectiveness_score = 0.0, commit is skipped."""
    async def run_test():
        patches = _base_patches()
        bug = _make_bug(bug_type="LINTING", sub_type="unused_import")

        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 1), \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "checkout_branch", return_value=True), \
             patch.object(orchestrator.git_agent, "push") as mock_push:

            # Both initial and verify runs return the SAME bugs → effectiveness = 0.0
            mock_exec.return_value = _fail_exec()
            mock_parser.return_value = [bug]
            mock_fix_agent.fix.return_value = _make_fix(bug, patch_fingerprint="fp_a")

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            # Push should NOT have been called (commit noise protection)
            mock_push.assert_not_called()
            assert state["effective_fix_count"] == 0

    asyncio.run(run_test())


# ===================================================================
# Test 2: CI polling timeout → ci_stalled_flag set, loop continues
# ===================================================================
def test_ci_polling_timeout_stalled(orchestrator, mock_fix_agent):
    """CI returns 'unknown_timeout' → flag set, loop continues."""
    async def run_test():
        patches = _base_patches()
        bug = _make_bug()

        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 2), \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "push"), \
             patch.object(orchestrator.git_agent, "get_last_commit_sha", return_value="sha1"), \
             patch.object(orchestrator.ci_monitor, "poll_status", new_callable=AsyncMock, return_value="unknown_timeout"):

            # Make bugs different between iterations to avoid repeated-fix detection
            bugs_iter1 = [_make_bug(file_path="a.py")]
            bugs_iter2 = [_make_bug(file_path="b.py")]
            # After fix verification, bugs change (effectiveness > 0 so commit goes through)
            mock_exec.side_effect = [
                _fail_exec(), _fail_exec(),  # iter1: build + verify
                _fail_exec(), _fail_exec(),  # iter2: build + verify
            ]
            mock_parser.side_effect = [
                bugs_iter1,      # iter1 parse
                [],              # iter1 verify parse → all bugs removed → effective
                bugs_iter2,      # iter2 parse
                [],              # iter2 verify parse → effective
            ]

            call_count = [0]
            def _unique_fix(*args, **kwargs):
                call_count[0] += 1
                bug_arg = kwargs.get("bug_report", args[0] if args else _make_bug())
                return _make_fix(bug_arg, patch_fingerprint=f"fp_{call_count[0]}")
            mock_fix_agent.fix.side_effect = _unique_fix

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            assert state["ci_stalled_flag"] is True
            assert len(state["ci_runs"]) >= 1
            assert state["ci_runs"][0].status == "unknown_timeout"
            # Loop should have continued past iteration 1
            assert state["iteration"] >= 2

    asyncio.run(run_test())


# ===================================================================
# Test 3: Regression detection (priority-aware)
# ===================================================================
def test_regression_detection_priority_aware():
    """New higher-priority failures → regressed. New lint only → improved."""
    # Case 1: SYNTAX removed, only LINTING remains → improved
    prev_bugs = [_make_bug(bug_type="SYNTAX")]
    curr_bugs = [_make_bug(bug_type="LINTING", sub_type="unused_var")]
    prev_sigs = [generate_bug_signature(b) for b in prev_bugs]
    curr_sigs = [generate_bug_signature(b) for b in curr_bugs]
    assert _classify_iteration_outcome(prev_sigs, curr_sigs, prev_bugs, curr_bugs) == "improved"

    # Case 2: Only LINTING before, SYNTAX appears → regressed
    prev_bugs2 = [_make_bug(bug_type="LINTING", sub_type="unused_var", file_path="utils.py")]
    curr_bugs2 = [_make_bug(bug_type="SYNTAX", sub_type="invalid_syntax", file_path="main.py")]
    prev_sigs2 = [generate_bug_signature(b) for b in prev_bugs2]
    curr_sigs2 = [generate_bug_signature(b) for b in curr_bugs2]
    assert _classify_iteration_outcome(prev_sigs2, curr_sigs2, prev_bugs2, curr_bugs2) == "regressed"

    # Case 3: Identical → unchanged
    prev_bugs3 = [_make_bug()]
    prev_sigs3 = [generate_bug_signature(b) for b in prev_bugs3]
    assert _classify_iteration_outcome(prev_sigs3, prev_sigs3, prev_bugs3, prev_bugs3) == "unchanged"


# ===================================================================
# Test 4: Fingerprint history cap
# ===================================================================
def test_fingerprint_history_cap():
    """After exceeding per-bug cap, oldest entries are dropped FIFO."""
    store = _FixHistoryStore(per_bug_cap=3, global_cap=5)

    # Add 5 fingerprints for same bug
    for idx in range(5):
        store.add("bug_A", f"fp_{idx}", idx)

    fps = store.get_fingerprints("bug_A")
    # Only last 3 should remain (per-bug cap)
    assert fps == {"fp_2", "fp_3", "fp_4"}
    assert "fp_0" not in fps
    assert "fp_1" not in fps

    # Global cap: add 5 unique bugs (cap is 5, so first should be evicted)
    for idx in range(5):
        store.add(f"other_bug_{idx}", f"fp_x_{idx}", idx)

    # "bug_A" should have been evicted (it was the oldest)
    assert store.get_fingerprints("bug_A") == set()
    assert store.tracked_signatures <= 5


# ===================================================================
# Test 5: Commit blocked when root failure remains
# ===================================================================
def test_root_commit_gate_blocks_lint_fix(orchestrator, mock_fix_agent):
    """SYNTAX failure remains → lint-only fix commit is blocked."""
    async def run_test():
        patches = _base_patches()

        # Bugs: SYNTAX + LINTING
        syntax_bug = _make_bug(bug_type="SYNTAX", file_path="main.py")
        lint_bug = _make_bug(bug_type="LINTING", sub_type="unused_var", file_path="utils.py", line_number=20)

        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 1), \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "checkout_branch", return_value=True), \
             patch.object(orchestrator.git_agent, "push") as mock_push:

            # Initial parse: both bugs
            # After fix: syntax still there, lint fixed → effective for lint but root remains
            mock_exec.side_effect = [_fail_exec(), _fail_exec()]
            mock_parser.side_effect = [
                [syntax_bug, lint_bug],  # initial
                [syntax_bug],            # verify: syntax remains, lint gone
            ]

            # Only lint bug gets fixed (syntax fix fails)
            async def _selective_fix(bug_report, **kwargs):
                if bug_report.bug_type == "SYNTAX":
                    return _make_fix(bug_report, success=False, escalation_reason="LOW_CONFIDENCE")
                return _make_fix(bug_report, bug_type="LINTING", patch_fingerprint="lint_fp")
            mock_fix_agent.fix.side_effect = _selective_fix

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            # Push should NOT have been called because root failure remains
            # and no fix targets root
            mock_push.assert_not_called()

    asyncio.run(run_test())


# ===================================================================
# Test 6: Effectiveness scoring uses bug_signature
# ===================================================================
def test_effectiveness_scoring_signature_based():
    """Score is computed from bug_signature, not message text."""
    bug = _make_bug()
    fix = _make_fix(bug)
    sig = generate_bug_signature(bug)

    # Bug removed from post-fix set → 1.0
    assert _score_effectiveness(fix, {sig}, set()) == 1.0
    # Bug unchanged → 0.0
    assert _score_effectiveness(fix, {sig}, {sig}) == 0.0
    # Bug not in pre but not in post either → 0.5 (partial)
    assert _score_effectiveness(fix, set(), set()) == 0.5


# ===================================================================
# Test 7: Global fingerprint cap evicts oldest signature
# ===================================================================
def test_global_fingerprint_cap():
    """When global cap reached, oldest bug signature is evicted."""
    store = _FixHistoryStore(per_bug_cap=5, global_cap=3)

    store.add("sig_1", "fp1", 1)
    store.add("sig_2", "fp2", 2)
    store.add("sig_3", "fp3", 3)
    # Now at capacity
    assert store.tracked_signatures == 3

    # Adding sig_4 should evict sig_1
    store.add("sig_4", "fp4", 4)
    assert store.tracked_signatures == 3
    assert store.get_fingerprints("sig_1") == set()
    assert store.get_fingerprints("sig_4") == {"fp4"}


# ===================================================================
# Test 8: Serialisation round-trip for FixHistoryStore
# ===================================================================
def test_fix_history_store_serialisation():
    """to_list / from_list round-trip preserves data."""
    store = _FixHistoryStore(per_bug_cap=5, global_cap=200)
    store.add("sig_a", "fp1", 1)
    store.add("sig_a", "fp2", 2)
    store.add("sig_b", "fp3", 3)

    serialised = store.to_list()
    assert len(serialised) == 3

    restored = _FixHistoryStore.from_list(serialised, per_bug_cap=5, global_cap=200)
    assert restored.get_fingerprints("sig_a") == {"fp1", "fp2"}
    assert restored.get_fingerprints("sig_b") == {"fp3"}
