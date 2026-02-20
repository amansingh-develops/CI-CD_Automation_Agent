"""
CI Monitor Agent
================
Polls GitHub Actions API to check the status of a specific commit/branch.
"""
import httpx
import time
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Literal, List, Dict, Any

logger = logging.getLogger(__name__)

CIStatus = Literal[
    "queued", 
    "in_progress", 
    "completed_success", 
    "completed_failure",
    "ci_error",
    "stalled", 
    "unknown_timeout"
]

class CIMonitor:
    """
    Agent that monitors the health of the CI/CD pipeline on GitHub.
    """

    def __init__(self, github_token: str = "") -> None:
        self.github_token = github_token
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "CI-Healing-Agent"
        }
        if github_token:
            self.headers["Authorization"] = f"token {github_token}"
        
        self.timeline: List[Dict[str, Any]] = []

    def _extract_repo_path(self, repo_url: str) -> str:
        """Extract 'owner/repo' from GitHub URL."""
        match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", repo_url)
        if match:
            return match.group(1).rstrip("/")
        return ""

    def _add_timeline_event(
        self,
        iteration: int,
        status: CIStatus,
        stalled_flag: bool = False,
        job_name: str = "",
        duration: float = 0.0,
    ) -> None:
        """Add a timeline event for the dashboard."""
        self.timeline.append({
            "iteration": iteration,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stalled_flag": stalled_flag,
            "job_name": job_name,
            "duration": round(duration, 2),
        })

    def _filter_jobs(self, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter jobs to focus on build/test and ignore deploy/publish."""
        ignore_keywords = ["deploy", "publish", "release", "notify"]
        focus_keywords = ["build", "test", "lint", "check", "ci"]
        
        filtered = []
        for job in jobs:
            name = job.get("name", "").lower()
            if any(kw in name for kw in ignore_keywords):
                continue
            if not focus_keywords or any(kw in name for kw in focus_keywords):
                filtered.append(job)
        
        # If no focus jobs found, return all non-ignored
        return filtered or jobs

    async def poll_status(
        self, 
        repo_url: str, 
        commit_sha: str, 
        timeout_seconds: int = 300,
        iteration: int = 1
    ) -> CIStatus:
        """
        Poll GitHub for check runs associated with a commit SHA.
        Retained for backwards compatibility with orchestrator.
        """
        repo_path = self._extract_repo_path(repo_url)
        if not repo_path:
            logger.error("Could not extract repo path from %s", repo_url)
            return "unknown_timeout"

        url = f"https://api.github.com/repos/{repo_path}/commits/{commit_sha}/check-runs"
        return await self._poll_url(url, "check_runs", timeout_seconds, iteration)

    async def poll_branch_status(
        self,
        repo_url: str,
        branch: str,
        timeout_seconds: int = 300,
        iteration: int = 1
    ) -> CIStatus:
        """
        Poll GitHub for workflow runs associated with a branch.
        """
        repo_path = self._extract_repo_path(repo_url)
        if not repo_path:
            logger.error("Could not extract repo path from %s", repo_url)
            return "unknown_timeout"

        url = f"https://api.github.com/repos/{repo_path}/actions/runs?branch={branch}"
        return await self._poll_url(url, "workflow_runs", timeout_seconds, iteration)

    async def _poll_url(self, url: str, data_key: str, timeout_seconds: int, iteration: int) -> CIStatus:
        """Shared polling logic with exponential backoff, stalled detection, and cooldown."""
        start_time = time.time()
        backoff = 5.0
        consecutive_in_progress = 0
        consecutive_same_state = 0
        last_status = ""

        async with httpx.AsyncClient(headers=self.headers, timeout=20.0) as client:
            while (time.time() - start_time) < timeout_seconds:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.json()
                    items = data.get(data_key, [])

                    if not items:
                        current_status: CIStatus = "queued"
                        job_names = ""
                    else:
                        # Apply job filtering logic
                        filtered_items = self._filter_jobs(items)
                        job_names = ", ".join(
                            r.get("name", "unknown")[:40] for r in filtered_items[:3]
                        )
                        
                        all_completed = all(r.get("status") == "completed" for r in filtered_items)
                        
                        if all_completed:
                            any_failed = any(
                                r.get("conclusion") in ("failure", "timed_out", "action_required", "cancelled") 
                                for r in filtered_items
                            )
                            status: CIStatus = "completed_failure" if any_failed else "completed_success"
                            elapsed = round(time.time() - start_time, 2)
                            self._add_timeline_event(
                                iteration, status, job_name=job_names, duration=elapsed
                            )
                            return status
                        
                        current_status = "in_progress"

                    # Stalled detection
                    if current_status == "in_progress":
                        consecutive_in_progress += 1
                        if consecutive_in_progress >= 7:  # ~2.5-3 minutes of no change
                            logger.warning("CI detected as STALLED")
                            elapsed = round(time.time() - start_time, 2)
                            self._add_timeline_event(
                                iteration, "stalled", stalled_flag=True,
                                job_name=job_names, duration=elapsed,
                            )
                            return "stalled"
                    else:
                        consecutive_in_progress = 0

                    # Polling cooldown — accelerate backoff on repeated same-state
                    if current_status == last_status:
                        consecutive_same_state += 1
                        # Use faster multiplier when stuck on same state
                        multiplier = 2.0 if consecutive_same_state >= 2 else 1.5
                    else:
                        consecutive_same_state = 0
                        multiplier = 1.5
                        elapsed = round(time.time() - start_time, 2)
                        logger.info("CI Status update: %s", current_status)
                        self._add_timeline_event(
                            iteration, current_status,
                            job_name=job_names if items else "",
                            duration=elapsed,
                        )
                        last_status = current_status

                    # Exponential backoff
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * multiplier, 30.0)

                except httpx.HTTPStatusError as http_err:
                    status_code = http_err.response.status_code
                    if 400 <= status_code < 500:
                        # 4xx = permanent error (commit doesn't exist, bad auth, etc.)
                        logger.error("CI polling aborted — HTTP %d: %s", status_code, http_err)
                        elapsed = round(time.time() - start_time, 2)
                        self._add_timeline_event(iteration, "ci_error", duration=elapsed)
                        return "ci_error"
                    # 5xx = transient, retry
                    logger.error("CI polling server error (HTTP %d), retrying: %s", status_code, http_err)
                    await asyncio.sleep(10)
                except Exception as e:
                    logger.error("Error polling CI: %s", e)
                    await asyncio.sleep(10)

        elapsed = round(time.time() - start_time, 2)
        self._add_timeline_event(iteration, "unknown_timeout", duration=elapsed)
        return "unknown_timeout"

    def get_timeline(self) -> List[Dict[str, Any]]:
        """Return the captured timeline events."""
        return self.timeline
