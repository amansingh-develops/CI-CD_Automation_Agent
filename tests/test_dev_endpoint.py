"""
Dev Endpoint Tests (Step 6.5)
==============================
Tests for POST /dev/run-repo diagnostic endpoint.
All orchestrator calls mocked — no real GitHub or Docker.
"""
import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.models.iteration_snapshot import IterationSnapshot


# ---------------------------------------------------------------------------
# Helpers — build a fake state dict that the orchestrator would return
# ---------------------------------------------------------------------------
def _fake_state(status="success", iterations=2, bugs=3, fixes=2, project_type="python"):
    snapshots = []
    for i in range(1, iterations + 1):
        snapshots.append(IterationSnapshot(
            iteration=i,
            bug_reports=[],
            ci_status="success" if i == iterations else "failure",
            iteration_outcome="improved" if i > 1 else "",
            effective_fix_count=1,
            skipped_fix_count=0,
            build_log_snippet="some log output",
            execution_summary=f"Iteration {i} done.",
        ))
    return {
        "repo_url": "https://github.com/test/repo",
        "project_type": project_type,
        "iteration": iterations,
        "status": status,
        "total_bugs_found": bugs,
        "total_fixes_applied": fixes,
        "commit_count": fixes,
        "ci_runs": [MagicMock(status="success")],
        "performance_hint": "normal",
        "snapshots": snapshots,
        "ci_stalled_flag": False,
        "fix_history": [],
        "execution_summary": "done",
    }


# ===================================================================
# Test 1: Endpoint returns 404 when disabled (default)
# ===================================================================
def test_dev_endpoint_disabled_returns_404():
    """When ENABLE_DEV_ENDPOINT is not set, return 404."""
    with patch("app.api.dev_run_repo._DEV_ENABLED", False):
        # Re-import to pick up the patched flag
        from main import app
        client = TestClient(app)
        resp = client.post("/dev/run-repo", json={
            "repo_url": "https://github.com/test/repo"
        })
        assert resp.status_code == 404


# ===================================================================
# Test 2: Endpoint triggers orchestrator when enabled
# ===================================================================
def test_dev_endpoint_enabled_triggers_run():
    """When enabled, endpoint calls orchestrator and returns summary."""
    with patch("app.api.dev_run_repo._DEV_ENABLED", True), \
         patch("app.api.dev_run_repo.GITHUB_TOKEN", "fake-token"), \
         patch("app.api.dev_run_repo.FixAgent") as mock_fa_cls, \
         patch("app.api.dev_run_repo.Orchestrator") as mock_orch_cls:

        mock_orch = MagicMock()
        mock_orch.run = AsyncMock(return_value=_fake_state())
        mock_orch_cls.return_value = mock_orch

        from main import app
        client = TestClient(app)
        resp = client.post("/dev/run-repo", json={
            "repo_url": "https://github.com/test/repo",
            "team_name": "TestTeam",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["final_status"] == "success"
        assert data["detected_project_type"] == "python"
        assert data["iteration_count"] == 2
        assert data["total_bug_reports_detected"] == 3
        assert data["total_fixes_applied"] == 2
        assert data["commit_count"] == 2
        assert len(data["iteration_outcomes"]) == 2
        assert "run_id" in data

        # Verify orchestrator was called
        mock_orch.run.assert_called_once()


# ===================================================================
# Test 3: Response matches schema
# ===================================================================
def test_dev_endpoint_response_schema():
    """Response includes all required fields."""
    with patch("app.api.dev_run_repo._DEV_ENABLED", True), \
         patch("app.api.dev_run_repo.GITHUB_TOKEN", "fake-token"), \
         patch("app.api.dev_run_repo.FixAgent"), \
         patch("app.api.dev_run_repo.Orchestrator") as mock_orch_cls:

        mock_orch = MagicMock()
        mock_orch.run = AsyncMock(return_value=_fake_state())
        mock_orch_cls.return_value = mock_orch

        from main import app
        client = TestClient(app)
        resp = client.post("/dev/run-repo", json={
            "repo_url": "https://github.com/test/repo"
        })

        data = resp.json()
        required_fields = [
            "run_id", "detected_project_type", "iteration_count",
            "final_status", "total_bug_reports_detected",
            "total_fixes_applied", "commit_count",
            "ci_polling_events_count", "performance_mode_last_iteration",
            "log_excerpt", "iteration_outcomes",
            "stalled_ci_detected", "repeated_fix_detected",
            "regression_detected",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"


# ===================================================================
# Test 4: Timeout override is respected and capped
# ===================================================================
def test_dev_endpoint_timeout_override():
    """timeout_override is capped at 480s."""
    with patch("app.api.dev_run_repo._DEV_ENABLED", True), \
         patch("app.api.dev_run_repo.GITHUB_TOKEN", "fake-token"), \
         patch("app.api.dev_run_repo.FixAgent"), \
         patch("app.api.dev_run_repo.Orchestrator") as mock_orch_cls, \
         patch("app.api.dev_run_repo.asyncio") as mock_asyncio:

        mock_orch = MagicMock()
        mock_orch.run = AsyncMock(return_value=_fake_state())
        mock_orch_cls.return_value = mock_orch
        # Make wait_for pass through
        mock_asyncio.wait_for = AsyncMock(return_value=_fake_state())
        mock_asyncio.TimeoutError = asyncio.TimeoutError

        from main import app
        client = TestClient(app)
        resp = client.post("/dev/run-repo", json={
            "repo_url": "https://github.com/test/repo",
            "timeout_override": 9999,  # way above cap
        })

        assert resp.status_code == 200
        # Verify wait_for was called with capped timeout (480)
        call_args = mock_asyncio.wait_for.call_args
        assert call_args[1].get("timeout", call_args[0][1] if len(call_args[0]) > 1 else None) <= 480


# ===================================================================
# Test 5: Rejects non-GitHub URLs
# ===================================================================
def test_dev_endpoint_rejects_non_github_url():
    """Non-GitHub URLs are rejected with 422."""
    with patch("app.api.dev_run_repo._DEV_ENABLED", True), \
         patch("app.api.dev_run_repo.GITHUB_TOKEN", "fake-token"):

        from main import app
        client = TestClient(app)
        resp = client.post("/dev/run-repo", json={
            "repo_url": "https://gitlab.com/some/repo"
        })
        assert resp.status_code == 422


# ===================================================================
# Test 6: Missing token returns 400
# ===================================================================
def test_dev_endpoint_missing_token():
    """No GITHUB_TOKEN → 400 error."""
    with patch("app.api.dev_run_repo._DEV_ENABLED", True), \
         patch("app.api.dev_run_repo.GITHUB_TOKEN", None):

        from main import app
        client = TestClient(app)
        resp = client.post("/dev/run-repo", json={
            "repo_url": "https://github.com/test/repo"
        })
        assert resp.status_code == 400


# ===================================================================
# Test 7: Observability flags populated correctly
# ===================================================================
def test_dev_endpoint_observability_flags():
    """Regression and stall flags are correctly surfaced."""
    state = _fake_state()
    state["ci_stalled_flag"] = True
    state["snapshots"][1].iteration_outcome = "regressed"

    with patch("app.api.dev_run_repo._DEV_ENABLED", True), \
         patch("app.api.dev_run_repo.GITHUB_TOKEN", "fake-token"), \
         patch("app.api.dev_run_repo.FixAgent"), \
         patch("app.api.dev_run_repo.Orchestrator") as mock_orch_cls:

        mock_orch = MagicMock()
        mock_orch.run = AsyncMock(return_value=state)
        mock_orch_cls.return_value = mock_orch

        from main import app
        client = TestClient(app)
        resp = client.post("/dev/run-repo", json={
            "repo_url": "https://github.com/test/repo"
        })

        data = resp.json()
        assert data["stalled_ci_detected"] is True
        assert data["regression_detected"] is True
