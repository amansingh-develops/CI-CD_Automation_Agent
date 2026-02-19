"""
Orchestrator Tests
==================
Tests the full healing loop with all external dependencies mocked.
"""
import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

from app.agents.orchestrator import Orchestrator
from app.agents.fix_agent import FixAgent
from app.models.bug_report import BugReport
from app.models.fix_result import FixResult
from app.executor.build_executor import ExecutionResult
from app.parser.classification import CONF_HIGH

import asyncio

@pytest.fixture
def mock_fix_agent():
    agent = MagicMock(spec=FixAgent)
    agent.fix = AsyncMock()
    return agent

@pytest.fixture
def orchestrator(mock_fix_agent):
    return Orchestrator(fix_agent=mock_fix_agent, github_token="fake-token")

def test_successful_first_attempt(orchestrator, mock_fix_agent):
    """If build passes on first try, loop should exit early with success."""
    async def run_test():
        with patch("app.agents.orchestrator.clone_repository", return_value="/fake/workspace"), \
             patch("app.agents.orchestrator.detect_project_type", return_value="python"), \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log", return_value=[]), \
             patch("app.agents.orchestrator.ResultsWriter.write_results") as mock_writer:
            
            # Mock a passing build (exit_code 0)
            mock_exec.return_value = ExecutionResult(
                exit_code=0,
                full_log="All tests passed!",
                log_excerpt="Passed"
            )
            
            state = await orchestrator.run(repo_url="https://github.com/org/repo")
            
            assert state["status"] == "success"
            assert state["iteration"] == 1
            assert len(state["snapshots"]) == 1
            assert state["total_fixes_applied"] == 0
            mock_writer.assert_called_once()
    
    asyncio.run(run_test())

def test_fix_then_pass(orchestrator, mock_fix_agent):
    """Build fails -> fix applied -> next iteration passes -> success."""
    async def run_test():
        with patch("app.agents.orchestrator.clone_repository", return_value="/fake/workspace"), \
             patch("app.agents.orchestrator.detect_project_type", return_value="python"), \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch("app.agents.orchestrator.ResultsWriter.write_results"), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="def hello(): pass")), \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "push"), \
             patch.object(orchestrator.git_agent, "get_last_commit_sha", return_value="sha123"), \
             patch.object(orchestrator.ci_monitor, "poll_status", return_value="success"):
            
            # Mock Iteration 1: Failure
            mock_exec.side_effect = [
                ExecutionResult(exit_code=1, full_log="FAIL", log_excerpt="FAIL"), # Iteration 1
                ExecutionResult(exit_code=0, full_log="PASS", log_excerpt="PASS")  # Iteration 2
            ]
            
            bug = BugReport(
                bug_type="SYNTAX", sub_type="invalid_syntax", 
                file_path="main.py", line_number=10, domain="python"
            )
            mock_parser.side_effect = [[bug], []]
            
            # Mock FixAgent response
            mock_fix_agent.fix.return_value = FixResult(
                bug_report=bug, success=True, patched_content="def fixed(): pass"
            )
            
            state = await orchestrator.run(repo_url="https://github.com/org/repo")
            
            assert state["status"] == "success"
            assert state["iteration"] == 1
            assert state["total_fixes_applied"] == 1
    
    asyncio.run(run_test())

def test_no_bugs_detected_early_exit(orchestrator, mock_fix_agent):
    """Build fails but parser finds zero bugs -> failure exit."""
    async def run_test():
        with patch("app.agents.orchestrator.clone_repository", return_value="/fake/workspace"), \
             patch("app.agents.orchestrator.detect_project_type", return_value="python"), \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log", return_value=[]), \
             patch("app.agents.orchestrator.ResultsWriter.write_results"):
            
            mock_exec.return_value = ExecutionResult(exit_code=1, full_log="weird error")
            
            state = await orchestrator.run(repo_url="https://github.com/org/repo")
            
            assert state["status"] == "failure"
            assert "identify specific bugs" in state["execution_summary"]
    
    asyncio.run(run_test())

def test_retries_exhausted(orchestrator, mock_fix_agent):
    """Loop exceeds RUN_RETRY_LIMIT -> exhausted status."""
    async def run_test():
        # Temporarily set RUN_RETRY_LIMIT to 2 for faster test
        with patch("app.agents.orchestrator.RUN_RETRY_LIMIT", 2), \
             patch("app.agents.orchestrator.clone_repository", return_value="/fake/workspace"), \
             patch("app.agents.orchestrator.detect_project_type", return_value="python"), \
             patch("app.agents.orchestrator.run_in_container") as mock_exec, \
             patch("app.agents.orchestrator.parse_failure_log") as mock_parser, \
             patch("app.agents.orchestrator.ResultsWriter.write_results"), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="content")), \
             patch.object(orchestrator.git_agent, "apply_fix", return_value=True), \
             patch.object(orchestrator.git_agent, "push"), \
             patch.object(orchestrator.git_agent, "get_last_commit_sha", return_value="sha"), \
             patch.object(orchestrator.ci_monitor, "poll_status", return_value="failure"):
            
            mock_exec.return_value = ExecutionResult(exit_code=1, full_log="FAIL")
            bug = BugReport(bug_type="LINTING", sub_type="unused", file_path="a.py", line_number=1)
            mock_parser.return_value = [bug]
            mock_fix_agent.fix.return_value = FixResult(bug_report=bug, success=True, patched_content="new")
            
            state = await orchestrator.run(repo_url="https://github.com/org/repo")
            
            assert state["status"] == "exhausted"
            assert state["iteration"] == 2
            assert len(state["snapshots"]) == 2
    
    asyncio.run(run_test())
