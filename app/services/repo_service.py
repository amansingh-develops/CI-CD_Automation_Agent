"""
Repo Service
============
Manages repository cloning and workspace lifecycle on the host machine.

Philosophy:
    - Clone ONCE into backend/workspace/<repo-name>/
    - Reuse the SAME workspace across all iterations.
    - Path normalization (absolute <-> relative).
"""
import os
import subprocess
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Base directory for all clones (on the host)
WORKSPACE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 
    "workspace"
)

def get_repo_name(repo_url: str) -> str:
    """Extract repository name from URL."""
    # Handle git@github.com:org/repo.git or https://github.com/org/repo
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name

def clone_repository(repo_url: str, github_token: str = "") -> str:
    """
    Clone a repository to the host workspace.
    Skip if directory already exists (reuse).

    Parameters
    ----------
    repo_url : str
        The repository URL to clone.
    github_token : str
        Optional GitHub token for private repos.

    Returns
    -------
    str
        Absolute path to the cloned repository.
    """
    if not os.path.exists(WORKSPACE_ROOT):
        os.makedirs(WORKSPACE_ROOT, exist_ok=True)

    repo_name = get_repo_name(repo_url)
    dest_path = os.path.abspath(os.path.join(WORKSPACE_ROOT, repo_name))

    if os.path.exists(dest_path):
        logger.info("Workspace already exists for %s at %s", repo_name, dest_path)
        return dest_path

    logger.info("Cloning %s into %s", repo_url, dest_path)
    
    # Insert token into URL if provided
    auth_url = repo_url
    if github_token and "github.com" in repo_url:
        if repo_url.startswith("https://"):
            auth_url = repo_url.replace("https://", f"https://x-access-token:{github_token}@")

    try:
        subprocess.run(
            ["git", "clone", auth_url, dest_path],
            check=True,
            capture_output=True,
            text=True
        )
        logger.info("Successfully cloned repository to %s", dest_path)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to clone repository: %s", e.stderr)
        raise RuntimeError(f"Cloning failed: {e.stderr}")

    return dest_path

def detect_project_type(workspace_path: str) -> str:
    """
    Detect the project type (node, python, generic) based on file presence.
    
    Priority:
        1. Node (package.json)
        2. Python (requirements.txt, pyproject.toml, etc.)
        3. Generic (Makefile, etc.)
        4. Fallback: generic
    """
    if os.path.exists(os.path.join(workspace_path, "package.json")):
        return "node"
    
    python_markers = ["requirements.txt", "pyproject.toml", "setup.py", "tox.ini", "Pipfile"]
    for marker in python_markers:
        if os.path.exists(os.path.join(workspace_path, marker)):
            return "python"
        
    if os.path.exists(os.path.join(workspace_path, "Makefile")):
        return "generic"
    
    return "generic"

def clean_workspace():
    """Wipe the entire workspace root (use with caution)."""
    import shutil
    if os.path.exists(WORKSPACE_ROOT):
        logger.info("Cleaning workspace root: %s", WORKSPACE_ROOT)
        shutil.rmtree(WORKSPACE_ROOT)
        os.makedirs(WORKSPACE_ROOT, exist_ok=True)
