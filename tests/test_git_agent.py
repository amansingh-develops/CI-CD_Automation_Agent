import pytest
import subprocess
import os
import re
from unittest.mock import MagicMock, patch, mock_open
from app.agents.git_agent import GitAgent
from app.models.fix_result import FixResult
from app.models.bug_report import BugReport

@pytest.fixture
def git_agent():
    return GitAgent()

def test_branch_name_generation(git_agent):
    branch = git_agent.generate_branch_name("My Team", "AI Agent")
    assert branch == "MY_TEAM_AI_AGENT_AI_Fix"
    assert git_agent.branch_name == "MY_TEAM_AI_AGENT_AI_Fix"

def test_branch_name_special_chars(git_agent):
    branch = git_agent.generate_branch_name("Team! @#$", "Agent 123")
    assert branch == "TEAM_AGENT_123_AI_Fix"

def test_branch_name_validation(git_agent):
    assert git_agent.validate_branch_name("TEAM_NAME_AI_Fix") is True
    assert git_agent.validate_branch_name("VALID_123_AI_Fix") is True
    assert git_agent.validate_branch_name("invalid") is False
    assert git_agent.validate_branch_name("team_ai_fix") is False # Lowercase invalid
    assert git_agent.validate_branch_name("TEAM AI FIX") is False

def test_commit_prefix_enforcement(git_agent):
    bug = BugReport(bug_type="SYNTAX", sub_type="error", file_path="main.py", line_number=1)
    fix = FixResult(bug_report=bug, success=True, patched_content="fixed")
    
    workspace = os.path.abspath("/tmp/repo")
    abs_file = os.path.join(workspace, "main.py")

    with patch("builtins.open", mock_open()), \
         patch("app.agents.git_agent.os.path.abspath", return_value=workspace), \
         patch("app.agents.git_agent.os.path.normpath", return_value=abs_file), \
         patch("subprocess.run") as mock_run:
            
            result = git_agent.apply_fix(fix, workspace)
            assert result is True
            
            # Check commit call
            commit_calls = [call for call in mock_run.call_args_list if "commit" in call.args[0]]
            assert len(commit_calls) > 0
            msg = commit_calls[0].args[0][3]
            assert msg.startswith("[AI-AGENT] Fix: SYNTAX/error")

def test_commit_cap_blocks_at_limit(git_agent):
    git_agent.commit_count = 20 # Assuming default limit is 20
    bug = BugReport(bug_type="LINTING", sub_type="unused", file_path="a.py", line_number=1)
    fix = FixResult(bug_report=bug, success=True, patched_content="new")
    
    result = git_agent.apply_fix(fix, "/tmp")
    assert result is False
    assert git_agent.efficiency_penalty_risk is True

@patch("subprocess.run")
def test_push_refuses_main(mock_run, git_agent):
    status = git_agent.push("/tmp", branch="main")
    assert status == "rejected_main"
    assert mock_run.call_count == 0

@patch("subprocess.run")
def test_push_retry_on_failure(mock_run, git_agent):
    git_agent.generate_branch_name("T", "A")
    # Fail first push, succeed fetch, rebase, then second push
    mock_run.side_effect = [
        subprocess.CalledProcessError(1, "git push", stderr="conflict"),
        MagicMock(),  # git fetch
        MagicMock(),  # git rebase
        MagicMock(),  # push 2 success
    ]
    
    with patch("time.sleep"):
        status = git_agent.push("/tmp")
        assert status == "success"
        assert mock_run.call_count == 4

def test_state_exposure(git_agent):
    git_agent.generate_branch_name("TEAM", "LEADER")
    git_agent.commit_count = 5
    git_agent.push_status = "success"
    
    state = git_agent.state
    assert state["branch_name"] == "TEAM_LEADER_AI_Fix"
    assert state["commit_count"] == 5
    assert state["push_status"] == "success"
    assert state["efficiency_penalty_risk"] is False
