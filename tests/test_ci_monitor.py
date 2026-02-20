import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from app.agents.ci_monitor import CIMonitor

@pytest.fixture
def monitor():
    return CIMonitor(github_token="fake")

def test_job_filtering(monitor):
    jobs = [
        {"name": "Build and Test", "status": "completed", "conclusion": "success"},
        {"name": "Deploy to Staging", "status": "completed", "conclusion": "success"},
        {"name": "Notify Slack", "status": "completed", "conclusion": "success"},
        {"name": "CI / lint", "status": "completed", "conclusion": "success"}
    ]
    
    filtered = monitor._filter_jobs(jobs)
    names = [j["name"] for j in filtered]
    
    assert "Build and Test" in names
    assert "CI / lint" in names
    assert "Deploy to Staging" not in names
    assert "Notify Slack" not in names

def test_exponential_backoff_logic(monitor):
    async def run_test():
        # Mock httpx client to return pending then success
        mock_resp_pending = MagicMock()
        mock_resp_pending.json.return_value = {"check_runs": [{"status": "in_progress", "name": "build"}]}
        mock_resp_pending.raise_for_status = MagicMock()
        
        mock_resp_success = MagicMock()
        mock_resp_success.json.return_value = {"check_runs": [{"status": "completed", "conclusion": "success", "name": "build"}]}
        mock_resp_success.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get") as mock_get, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            
            mock_get.side_effect = [mock_resp_pending, mock_resp_success]
            
            status = await monitor.poll_status("https://github.com/o/r", "sha123")
            
            assert status == "completed_success"
            # Check if sleep was called with initial backoff
            mock_sleep.assert_called_once_with(5.0)
    
    asyncio.run(run_test())

def test_stalled_detection(monitor):
    async def run_test():
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"check_runs": [{"status": "in_progress", "name": "build"}]}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", return_value=mock_resp), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            
            # Should return stalled after 7 consecutive in_progress
            status = await monitor.poll_status("https://github.com/o/r", "sha123")
            assert status == "stalled"
            assert any(e["status"] == "stalled" for e in monitor.timeline)
    
    asyncio.run(run_test())

def test_timeline_generation(monitor):
    async def run_test():
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"check_runs": [{"status": "completed", "conclusion": "failure", "name": "build"}]}
        mock_resp.raise_for_status = MagicMock()
        
        with patch("httpx.AsyncClient.get", return_value=mock_resp):
            await monitor.poll_status("https://github.com/o/r", "sha", iteration=2)
            
            timeline = monitor.get_timeline()
            assert len(timeline) > 0
            assert timeline[-1]["iteration"] == 2
            assert timeline[-1]["status"] == "completed_failure"
    
    asyncio.run(run_test())

def test_max_polling_window(monitor):
    async def run_test():
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"check_runs": []} # Stay 'queued'
        mock_resp.raise_for_status = MagicMock()

        # Use a counter-based mock so time.time never runs out of values
        call_count = {"n": 0}
        def fake_time():
            call_count["n"] += 1
            # First call returns 0 (start_time), second+ return 301 (past timeout)
            return 0.0 if call_count["n"] <= 1 else 301.0

        with patch("httpx.AsyncClient.get", return_value=mock_resp), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("time.time", side_effect=fake_time):
            
            status = await monitor.poll_status("https://github.com/o/r", "sha", timeout_seconds=300)
            assert status == "unknown_timeout"
    
    asyncio.run(run_test())
