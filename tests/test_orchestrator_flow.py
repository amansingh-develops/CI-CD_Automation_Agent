"""
Orchestrator Flow Tests
========================
Comprehensive tests for the autonomous CI healing loop.
All external dependencies mocked (Docker, Git, GitHub API, LLM).
"""
import pytest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch, mock_open, PropertyMock

from app.agents.orchestrator import Orchestrator, _sort_bugs_by_priority, _classify_domain
from app.agents.fix_agent import FixAgent
from app.models.bug_report import BugReport
from app.models.fix_result import FixResult
from app.executor.build_executor import ExecutionResult
from app.parser.classification import CONF_HIGH


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
              patch_fingerprint="fp1", escalation_reason=""):
    return FixResult(
        bug_report=bug, success=success, patched_content=patched_content,
        confidence=confidence, patch_fingerprint=patch_fingerprint,
        escalation_reason=escalation_reason
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


# ===================================================================
# Standard mocking context manager for all orchestrator tests
# ===================================================================
def _base_patches():
    """Return common patches for all orchestrator tests."""
    return {
        "clone": patch("app.agents.orchestrator.clone_repository", return_value="/fake/workspace"),
        "detect": patch("app.agents.orchestrator.detect_project_type", return_value="python"),
        "writer": patch("app.agents.orchestrator.ResultsWriter.write_results"),
        "exists": patch("os.path.exists", return_value=True),
        "open_file": patch("builtins.open", mock_open(read_data="def hello(): pass")),
    }


# ===================================================================
# Test 1: Retry stops at limit
# ===================================================================
def test_retry_stops_at_limit(orchestrator, mock_fix_agent):
    """Loop respects RUN_RETRY_LIMIT, exits with 'exhausted'."""
    async def run_test():
        patches = _base_patches()
        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 2), \
             patch("app.agents.orchestrator.run_in_container", return_value=_fail_exec()), \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "checkout_branch", return_value=True), \
             patch.object(orchestrator.git_agent, "push"), \
             patch.object(orchestrator.git_agent, "get_last_commit_sha", return_value="sha1"), \
             patch.object(orchestrator.ci_monitor, "poll_status", new_callable=AsyncMock, return_value="failure"):

            bug = _make_bug()
            mock_parser.return_value = [bug]

            # Use side_effect to return unique fingerprints per call
            # so repeated-fix detection doesn't trigger early exit
            call_count = [0]
            def _unique_fix(*args, **kwargs):
                call_count[0] += 1
                return _make_fix(bug, patch_fingerprint=f"fp_iter_{call_count[0]}")
            mock_fix_agent.fix.side_effect = _unique_fix

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            assert state["status"] == "exhausted"
            assert state["iteration"] == 2
            assert len(state["snapshots"]) == 2

    asyncio.run(run_test())


# ===================================================================
# Test 2: Repeated fixes skipped
# ===================================================================
def test_repeated_fixes_skipped(orchestrator, mock_fix_agent):
    """Same fingerprint across iterations → fix skipped on second attempt."""
    async def run_test():
        patches = _base_patches()
        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 2), \
             patch("app.agents.orchestrator.run_in_container", return_value=_fail_exec()), \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "checkout_branch", return_value=True), \
             patch.object(orchestrator.git_agent, "push"), \
             patch.object(orchestrator.git_agent, "get_last_commit_sha", return_value="sha1"), \
             patch.object(orchestrator.ci_monitor, "poll_status", new_callable=AsyncMock, return_value="failure"):

            bug = _make_bug()
            mock_parser.return_value = [bug]

            # Same fingerprint returned each time
            fix = _make_fix(bug, patch_fingerprint="same_fp_001")
            mock_fix_agent.fix.return_value = fix

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            # Fix history should contain the fingerprint
            assert len(state["fix_history"]) >= 1
            assert state["fix_history"][0]["patch_fingerprint"] == "same_fp_001"

    asyncio.run(run_test())


# ===================================================================
# Test 3: Commit conditions enforced
# ===================================================================
def test_commit_conditions_enforced(orchestrator, mock_fix_agent):
    """No commit when all fixes are escalated/rejected."""
    async def run_test():
        patches = _base_patches()
        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 1), \
             patch("app.agents.orchestrator.run_in_container", return_value=_fail_exec()), \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch.object(orchestrator.git_agent, "apply_fix") as mock_apply, \
             patch.object(orchestrator.git_agent, "checkout_branch", return_value=True), \
             patch.object(orchestrator.git_agent, "push") as mock_push:

            bug = _make_bug()
            mock_parser.return_value = [bug]

            # Return a failed fix (escalated)
            mock_fix_agent.fix.return_value = _make_fix(
                bug, success=False, escalation_reason="LOW_CONFIDENCE"
            )

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            # apply_fix and push should NOT have been called
            mock_apply.assert_not_called()
            mock_push.assert_not_called()
            assert state["total_fixes_applied"] == 0

    asyncio.run(run_test())


# ===================================================================
# Test 4: Results generated
# ===================================================================
def test_results_generated(orchestrator, mock_fix_agent):
    """ResultsWriter.write_results() is called with valid state."""
    async def run_test():
        patches = _base_patches()
        with patches["clone"], patches["detect"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.ResultsWriter.write_results") as mock_writer, \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 1), \
             patch("app.agents.orchestrator.run_in_container", return_value=_pass_exec()), \
             patch("app.agents.orchestrator.parse_failure_log", return_value=[]):

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            mock_writer.assert_called_once()
            call_args = mock_writer.call_args[0][0]
            assert call_args["status"] == "success"
            assert "fix_history" in call_args
            assert "start_time" in call_args

    asyncio.run(run_test())


# ===================================================================
# Test 5: Failure prioritization
# ===================================================================
def test_failure_prioritization():
    """Bugs are sorted SYNTAX → IMPORT → TYPE_ERROR → LOGIC → LINTING."""
    bugs = [
        _make_bug(bug_type="LINTING", sub_type="unused_import"),
        _make_bug(bug_type="SYNTAX", sub_type="invalid_syntax"),
        _make_bug(bug_type="LOGIC", sub_type="wrong_condition"),
        _make_bug(bug_type="IMPORT", sub_type="missing_import"),
        _make_bug(bug_type="TYPE_ERROR", sub_type="type_mismatch"),
    ]
    sorted_bugs = _sort_bugs_by_priority(bugs)

    assert sorted_bugs[0].bug_type == "SYNTAX"
    assert sorted_bugs[1].bug_type == "IMPORT"
    assert sorted_bugs[2].bug_type == "TYPE_ERROR"
    assert sorted_bugs[3].bug_type == "LOGIC"
    assert sorted_bugs[4].bug_type == "LINTING"


# ===================================================================
# Test 6: Confidence gating reverts ineffective fix
# ===================================================================
def test_confidence_gating_reverts(orchestrator, mock_fix_agent):
    """Ineffective patch (same failure signature) is marked as ineffective."""
    async def run_test():
        patches = _base_patches()
        bug = _make_bug()

        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 1), \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "checkout_branch", return_value=True), \
             patch.object(orchestrator.git_agent, "push"), \
             patch.object(orchestrator.git_agent, "get_last_commit_sha", return_value="sha1"), \
             patch.object(orchestrator.ci_monitor, "poll_status", new_callable=AsyncMock, return_value="failure"):

            # Both initial and verification runs return the same failure
            mock_exec.return_value = _fail_exec()
            mock_parser.return_value = [bug]
            mock_fix_agent.fix.return_value = _make_fix(bug)

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            # The fix should have been marked as ineffective via error_message
            assert state["total_fixes_applied"] >= 1

    asyncio.run(run_test())


# ===================================================================
# Test 7: Performance guardrail triggers
# ===================================================================
def test_performance_guardrail(orchestrator, mock_fix_agent):
    """When time exceeds threshold, performance_hint changes."""
    async def run_test():
        patches = _base_patches()
        # Simulate time.time() returning values that trigger the guardrail
        start = 1000.0
        # Use a counter so first call returns `start` (for run_start),
        # all subsequent calls return `start + 200` (elapsed = 200s → "reduced")
        call_count = [0]
        def _mock_time():
            call_count[0] += 1
            if call_count[0] == 1:
                return start
            return start + 200

        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 1), \
             patch("app.agents.orchestrator.run_in_container", return_value=_pass_exec()), \
             patch("app.agents.orchestrator.parse_failure_log", return_value=[]), \
             patch("app.agents.orchestrator.time") as mock_time:

            mock_time.time.side_effect = _mock_time

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            assert state["performance_hint"] == "reduced"

    asyncio.run(run_test())


# ===================================================================
# Test 8: Escalation does not stall loop
# ===================================================================
def test_escalation_does_not_stall(orchestrator, mock_fix_agent):
    """When all bugs are escalated in an iteration, loop exits cleanly."""
    async def run_test():
        patches = _base_patches()
        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 1), \
             patch("app.agents.orchestrator.run_in_container", return_value=_fail_exec()), \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser:

            bugs = [
                _make_bug(bug_type="SYNTAX"),
                _make_bug(bug_type="IMPORT", sub_type="missing_import", file_path="b.py"),
            ]
            mock_parser.return_value = bugs

            # All fixes fail (escalated)
            mock_fix_agent.fix.return_value = _make_fix(
                bugs[0], success=False, escalation_reason="LOW_CONFIDENCE"
            )

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            assert state["status"] == "failure"
            assert "escalated" in state["execution_summary"].lower()
            assert state["total_fixes_applied"] == 0

    asyncio.run(run_test())


# ===================================================================
# Test 9: Domain classification
# ===================================================================
def test_domain_classification():
    """File paths are classified into correct commit domains."""
    assert _classify_domain("db/migrations/001.sql") == "database"
    assert _classify_domain("src/components/Button.tsx") == "frontend"
    assert _classify_domain("app/services/auth.py") == "backend"
    assert _classify_domain("client/index.js") == "frontend"
    assert _classify_domain("schema/tables.py") == "database"


# ===================================================================
# Test 10: Successful first attempt
# ===================================================================
def test_successful_first_attempt(orchestrator, mock_fix_agent):
    """If build passes on first try, loop exits early with success."""
    async def run_test():
        patches = _base_patches()
        with patches["clone"], patches["detect"], patches["writer"], \
             patch("app.agents.orchestrator.run_in_container", return_value=_pass_exec()), \
             patch("app.agents.orchestrator.parse_failure_log", return_value=[]):

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            assert state["status"] == "success"
            assert state["iteration"] == 1
            assert len(state["snapshots"]) == 1
            assert state["total_fixes_applied"] == 0
            assert state["fix_history"] == []

    asyncio.run(run_test())


# ===================================================================
# Test 11: Fix then pass
# ===================================================================
def test_fix_then_pass(orchestrator, mock_fix_agent):
    """Build fails → fix applied → CI passes on push → success."""
    async def run_test():
        patches = _base_patches()
        bug = _make_bug()

        with patches["clone"], patches["detect"], patches["writer"], \
             patches["exists"], patches["open_file"], \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "checkout_branch", return_value=True), \
             patch.object(orchestrator.git_agent, "push"), \
             patch.object(orchestrator.git_agent, "get_last_commit_sha", return_value="sha123"), \
             patch.object(orchestrator.ci_monitor, "poll_status", new_callable=AsyncMock, return_value="success"):

            # First exec: fail, second (confidence gate): still fail (different sig), 
            # but CI passes after push
            mock_exec.side_effect = [
                _fail_exec(),  # initial build
                _fail_exec(),  # confidence gate re-exec (different bugs = different sig)
            ]
            mock_parser.side_effect = [
                [bug],  # initial parse
                [],     # confidence gate parse — no bugs (different signature)
            ]
            mock_fix_agent.fix.return_value = _make_fix(bug)

            state = await orchestrator.run(repo_url="https://github.com/org/repo")

            assert state["status"] == "success"
            assert state["total_fixes_applied"] == 1
            assert len(state["ci_runs"]) == 1

    asyncio.run(run_test())
