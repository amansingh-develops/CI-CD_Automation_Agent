"""
Git Agent
=========
Handles repository operations: writing fixes, committing, and pushing.
Enforces strict naming conventions and safety limits.
"""
import os
import subprocess
import logging
from typing import Optional
from app.models.fix_result import FixResult

logger = logging.getLogger(__name__)

MAX_COMMITS_PER_RUN = 20

class GitAgent:
    """
    Agent responsible for applying code changes to the local workspace
    and pushing them to the remote repository.
    """

    def __init__(self, commit_prefix: str = "[AI-AGENT]") -> None:
        self.commit_prefix = commit_prefix
        self.commit_count = 0

    def apply_fix(self, fix_result: FixResult, workspace_path: str) -> bool:
        """
        Write the patched content to disk and commit it.
        
        Parameters
        ----------
        fix_result : FixResult
            The generated fix to apply.
        workspace_path : str
            Absolute path to the repository on the host.

        Returns
        -------
        bool
            True if applied and committed successfully.
        """
        if not fix_result.success or not fix_result.patched_content:
            logger.warning("Refusing to apply unsuccessful or empty fix")
            return False

        if self.commit_count >= MAX_COMMITS_PER_RUN:
            logger.error("Max commit limit reached (%d), blocking further fixes", MAX_COMMITS_PER_RUN)
            return False

        repo_relative_path = fix_result.bug_report.file_path
        abs_path = os.path.normpath(os.path.join(workspace_path, repo_relative_path))

        # Security check: ensure path is within workspace
        if not abs_path.startswith(os.path.abspath(workspace_path)):
            logger.error("Security violation: attempt to write outside workspace: %s", abs_path)
            return False

        try:
            # 1. Write the file
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(fix_result.patched_content)
            
            # 2. Stage and commit
            bug = fix_result.bug_report
            commit_msg = (
                f"{self.commit_prefix} Fix: {bug.bug_type}/{bug.sub_type} "
                f"in {bug.file_path}"
            )

            # git add <file>
            subprocess.run(
                ["git", "add", repo_relative_path],
                cwd=workspace_path,
                check=True,
                capture_output=True,
                text=True
            )

            # git commit -m <msg>
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=workspace_path,
                check=True,
                capture_output=True,
                text=True
            )

            self.commit_count += 1
            logger.info("Successfully committed fix: %s", commit_msg)
            return True

        except Exception as e:
            logger.error("Failed to apply/commit fix for %s: %s", repo_relative_path, e)
            return False

    def push(self, workspace_path: str, branch: str = "main") -> bool:
        """
        Push local commits to the remote repository.
        """
        try:
            subprocess.run(
                ["git", "push", "origin", branch],
                cwd=workspace_path,
                check=True,
                capture_output=True,
                text=True
            )
            logger.info("Successfully pushed changes to branch: %s", branch)
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Failed to push changes: %s", e.stderr)
            return False

    def get_last_commit_sha(self, workspace_path: str) -> str:
        """Get the SHA of the HEAD commit."""
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace_path,
                check=True,
                capture_output=True,
                text=True
            )
            return res.stdout.strip()
        except Exception:
            return ""
