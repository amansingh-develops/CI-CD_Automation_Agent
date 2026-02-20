"""
Step 7.1 — Reliability Refinement Tests
========================================
Tests for provider cooldown, patch truncation, priority commit gating,
CI timeline clarity, push conflict safety, and telemetry exposure.
"""
import pytest
import asyncio
import subprocess
import time
from unittest.mock import MagicMock, patch, mock_open, AsyncMock

from app.llm.router import LLMRouter, ProviderHealth
from app.agents.git_agent import GitAgent, BUG_PRIORITY_TIERS
from app.agents.ci_monitor import CIMonitor
from app.models.fix_result import FixResult
from app.models.bug_report import BugReport


# -----------------------------------------------------------------------
# Provider Cooldown Tests
# -----------------------------------------------------------------------
class TestProviderCooldown:
    def test_cooldown_triggered_after_threshold(self):
        """Provider becomes unhealthy after N consecutive failures."""
        router = LLMRouter()
        for _ in range(4):
            router.report_failure("groq")
        
        health = router.get_health("groq")
        assert health.is_healthy is False
        assert health.cooldown_remaining == 5  # default skip count

    def test_cooldown_auto_reenable(self):
        """Provider re-enables after cooldown ticks expire."""
        router = LLMRouter()
        # Trigger cooldown
        for _ in range(4):
            router.report_failure("groq")
        
        health = router.get_health("groq")
        assert health.is_healthy is False
        
        # Tick cooldown 5 times (skip_count=5), simulated via get_provider calls
        for _ in range(5):
            router.get_provider()
        
        assert health.is_healthy is True
        assert health.consecutive_failures > 0

    def test_provider_health_state_exposed(self):
        """provider_health_state property returns per-provider status."""
        router = LLMRouter()
        router.report_failure("gemini")
        
        state = router.provider_health_state
        assert "gemini" in state
        assert "groq" in state
        assert state["gemini"]["consecutive_failures"] == 1
        assert state["groq"]["is_healthy"] is True

    def test_provider_usage_telemetry(self):
        """Usage log records provider, fallback, and cooldown flags."""
        router = LLMRouter()
        router.log_provider_usage("gemini", fallback_triggered=False, cooldown_active=False)
        router.log_provider_usage("groq", fallback_triggered=True, cooldown_active=True)
        
        log = router.get_provider_usage_log()
        assert len(log) == 2
        assert log[0]["provider_used"] == "gemini"
        assert log[1]["fallback_triggered"] is True
        assert log[1]["cooldown_active"] is True

    def test_reset_clears_usage_log(self):
        """Reset clears usage log and cooldown state."""
        router = LLMRouter()
        router.log_provider_usage("gemini")
        for _ in range(4):
            router.report_failure("groq")
        
        router.reset()
        
        assert len(router.get_provider_usage_log()) == 0
        health = router.get_health("groq")
        assert health.is_healthy is True
        assert health.cooldown_remaining == 0


# -----------------------------------------------------------------------
# Patch Truncation Guard Tests
# -----------------------------------------------------------------------
class TestPatchTruncation:
    def test_truncated_patch_rejected(self):
        """Patch < 30% of original length is rejected."""
        agent = GitAgent()
        original = "x" * 1000
        truncated = "x" * 100  # 10% — too small
        
        assert agent.validate_patch_size(original, truncated) is False

    def test_acceptable_patch_passes(self):
        """Patch >= 30% of original length is accepted."""
        agent = GitAgent()
        original = "x" * 1000
        patched = "x" * 500  # 50% — fine
        
        assert agent.validate_patch_size(original, patched) is True

    def test_empty_original_always_passes(self):
        """Empty original file means nothing to compare against."""
        agent = GitAgent()
        assert agent.validate_patch_size("", "new content") is True

    def test_validate_patch_size_still_available(self):
        """validate_patch_size static method is still available for optional use."""
        agent = GitAgent()
        assert agent.validate_patch_size("x" * 100, "x" * 10) is False
        assert agent.validate_patch_size("x" * 100, "x" * 50) is True


# -----------------------------------------------------------------------
# Priority Commit Gating Tests
# -----------------------------------------------------------------------
class TestPriorityGating:
    def test_syntax_fixed_allows_commit(self):
        """Commit allowed when SYNTAX fixed but LINTING remains."""
        agent = GitAgent()
        previous = ["SYNTAX", "LINTING"]
        current = ["LINTING"]  # SYNTAX gone
        
        assert agent.should_commit_by_priority(current, previous) is True
        assert agent.commit_priority_delta > 0

    def test_no_improvement_blocks_commit(self):
        """Commit blocked when no tier improvement."""
        agent = GitAgent()
        previous = ["LINTING"]
        current = ["LINTING"]
        
        assert agent.should_commit_by_priority(current, previous) is False

    def test_priority_tiers_defined(self):
        """All expected bug types have priority tiers."""
        assert BUG_PRIORITY_TIERS["SYNTAX"] < BUG_PRIORITY_TIERS["LINTING"]
        assert BUG_PRIORITY_TIERS["IMPORT"] < BUG_PRIORITY_TIERS["LOGIC"]

    def test_state_exposes_priority_delta(self):
        """State includes commit_priority_delta after computation."""
        agent = GitAgent()
        agent.compute_priority_delta(["LINTING"], ["SYNTAX", "LINTING"])
        
        state = agent.state
        assert "commit_priority_delta" in state
        assert state["commit_priority_delta"] > 0


# -----------------------------------------------------------------------
# Commit Budget Telemetry Tests
# -----------------------------------------------------------------------
class TestCommitBudget:
    def test_remaining_budget_exposed(self):
        """State exposes remaining_commit_budget."""
        agent = GitAgent()
        agent.commit_count = 15
        
        state = agent.state
        assert state["remaining_commit_budget"] == 5
        assert state["commit_budget_risk"] is False

    def test_budget_risk_flag_near_limit(self):
        """commit_budget_risk is True when <= 3 commits remain."""
        agent = GitAgent()
        agent.commit_count = 18  # 2 remaining (cap=20)
        
        state = agent.state
        assert state["remaining_commit_budget"] == 2
        assert state["commit_budget_risk"] is True


# -----------------------------------------------------------------------
# Push Conflict Safety Tests
# -----------------------------------------------------------------------
class TestPushConflictSafety:
    @patch("subprocess.run")
    def test_push_conflict_triggers_rebase(self, mock_run):
        """Push failure triggers fetch + rebase before retry."""
        agent = GitAgent()
        agent.generate_branch_name("T", "A")
        
        # First push fails, fetch succeeds, rebase succeeds, second push succeeds
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git push", stderr="conflict"),  # push 1
            MagicMock(),  # git fetch
            MagicMock(),  # git rebase
            MagicMock(),  # push 2 (success)
        ]
        
        with patch("time.sleep"):
            status = agent.push("/tmp")
        
        assert status == "success"
        assert mock_run.call_count == 4
        # Verify fetch and rebase were called
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ["git", "fetch", "origin", agent.branch_name] in calls
        assert ["git", "rebase", f"origin/{agent.branch_name}"] in calls

    @patch("subprocess.run")
    def test_push_conflict_unresolved_on_rebase_failure(self, mock_run):
        """Rebase failure results in conflict_unresolved, no crash."""
        agent = GitAgent()
        agent.generate_branch_name("T", "A")
        
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, "git push", stderr="conflict"),
            subprocess.CalledProcessError(1, "git fetch", stderr="fetch fail"),
        ]
        
        status = agent.push("/tmp")
        assert status == "conflict_unresolved"


# -----------------------------------------------------------------------
# CI Timeline Clarity Tests
# -----------------------------------------------------------------------
class TestCITimelineClarity:
    def test_timeline_includes_job_name_and_duration(self):
        """Timeline events include job_name and duration fields."""
        monitor = CIMonitor(github_token="fake")
        
        async def run_test():
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "check_runs": [{
                    "status": "completed",
                    "conclusion": "success",
                    "name": "Build Python"
                }]
            }
            mock_resp.raise_for_status = MagicMock()
            
            with patch("httpx.AsyncClient.get", return_value=mock_resp):
                await monitor.poll_status("https://github.com/o/r", "sha", iteration=1)
        
        asyncio.run(run_test())
        
        timeline = monitor.get_timeline()
        assert len(timeline) > 0
        event = timeline[-1]
        assert "job_name" in event
        assert "duration" in event
        assert event["duration"] >= 0
        assert "Build Python" in event["job_name"]

    def test_stalled_event_has_stalled_flag(self):
        """Stalled detection produces event with stalled_flag=True."""
        monitor = CIMonitor(github_token="fake")
        
        async def run_test():
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "check_runs": [{"status": "in_progress", "name": "CI Tests"}]
            }
            mock_resp.raise_for_status = MagicMock()
            
            with patch("httpx.AsyncClient.get", return_value=mock_resp), \
                 patch("asyncio.sleep", new_callable=AsyncMock):
                await monitor.poll_status("https://github.com/o/r", "sha")
        
        asyncio.run(run_test())
        
        stalled_events = [e for e in monitor.timeline if e["stalled_flag"]]
        assert len(stalled_events) >= 1
        assert stalled_events[0]["status"] == "stalled"
        assert "CI Tests" in stalled_events[0]["job_name"]
