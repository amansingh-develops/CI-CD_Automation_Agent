"""
Project Detector
================
Detects the project type from repository signals (marker files).

Detection is deterministic — same repo always yields the same project type.
Detection runs once after clone; result is cached for subsequent iterations.
No LLM is used. Pure heuristic matching only.
"""
import os
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Signal File → Project Type mapping (ordered by priority)
# ---------------------------------------------------------------------------
# Order matters: first match wins. More specific signals checked first.
SIGNAL_MAP: list[tuple[str, str]] = [
    ("package.json",     "node"),
    ("requirements.txt", "python"),
    ("pyproject.toml",   "python"),
    ("setup.py",         "python"),
    ("pom.xml",          "java"),
    ("build.gradle",     "java"),
    ("go.mod",           "go"),
    ("Cargo.toml",       "rust"),
    ("Dockerfile",       "docker_project"),
]


def detect_project_type(workspace_path: str) -> Optional[str]:
    """
    Scan the workspace root for signal files and return the project type.

    Parameters
    ----------
    workspace_path : str
        Absolute path to the cloned repository root.

    Returns
    -------
    str | None
        Detected project type string (e.g. "python", "node", "java"),
        or None if no signal file is found.

    Notes
    -----
    - Checks files in SIGNAL_MAP order; first match wins.
    - Only checks the workspace root directory (no recursive search).
    - Deterministic: same directory contents → same result.
    """
    if not os.path.isdir(workspace_path):
        return None

    for signal_file, project_type in SIGNAL_MAP:
        if os.path.isfile(os.path.join(workspace_path, signal_file)):
            return project_type

    return None


def detect_all_signals(workspace_path: str) -> list[str]:
    """
    Return all signal files found in the workspace root.

    Useful for debugging and for multi-language repos where
    more than one signal file exists.

    Parameters
    ----------
    workspace_path : str
        Absolute path to the cloned repository root.

    Returns
    -------
    list[str]
        List of signal file names found (e.g. ["package.json", "Dockerfile"]).
    """
    if not os.path.isdir(workspace_path):
        return []

    found = []
    for signal_file, _ in SIGNAL_MAP:
        if os.path.isfile(os.path.join(workspace_path, signal_file)):
            found.append(signal_file)
    return found


# ---------------------------------------------------------------------------
# Multi-Project Detection (for monorepos / full-stack projects)
# ---------------------------------------------------------------------------
@dataclass
class ProjectContext:
    """
    A detected sub-project within a repository.

    Attributes
    ----------
    path : str
        Relative path from workspace root (e.g. "client", "server", ".").
    project_type : str
        Detected project type (e.g. "node", "python").
    signal_file : str
        The signal file that triggered detection.
    """
    path: str
    project_type: str
    signal_file: str


# Directories to skip when scanning for sub-projects
_SKIP_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".git", ".tox", ".mypy_cache", ".pytest_cache", "site-packages",
    ".next", ".nuxt", "target", "vendor", "pkg", "bin", "obj",
}


def detect_multi_project(workspace_path: str, max_depth: int = 2) -> list[ProjectContext]:
    """
    Scan workspace root AND subdirectories (up to max_depth) for projects.

    This handles monorepos and full-stack projects with separate
    frontend/backend directories.

    Parameters
    ----------
    workspace_path : str
        Absolute path to the repository root.
    max_depth : int
        Maximum depth to scan (1 = root only, 2 = root + immediate children).

    Returns
    -------
    list[ProjectContext]
        All detected sub-projects, ordered by path depth then alphabetically.
        Root project (path=".") appears first if present.

    Examples
    --------
    A Next.js + FastAPI repo might return::

        [
            ProjectContext(path=".", project_type="docker_project", signal_file="Dockerfile"),
            ProjectContext(path="client", project_type="node", signal_file="package.json"),
            ProjectContext(path="server", project_type="python", signal_file="requirements.txt"),
        ]
    """
    if not os.path.isdir(workspace_path):
        return []

    contexts: list[ProjectContext] = []
    scanned_dirs: set[str] = set()

    def _scan_dir(dir_path: str, rel_path: str, depth: int) -> None:
        if depth > max_depth or dir_path in scanned_dirs:
            return
        scanned_dirs.add(dir_path)

        for signal_file, project_type in SIGNAL_MAP:
            if os.path.isfile(os.path.join(dir_path, signal_file)):
                contexts.append(ProjectContext(
                    path=rel_path if rel_path else ".",
                    project_type=project_type,
                    signal_file=signal_file,
                ))
                break  # One type per directory

        # Recurse into subdirectories
        if depth < max_depth:
            try:
                entries = sorted(os.listdir(dir_path))
            except OSError:
                return
            for entry in entries:
                if entry in _SKIP_DIRS or entry.startswith("."):
                    continue
                child_path = os.path.join(dir_path, entry)
                if os.path.isdir(child_path):
                    child_rel = f"{rel_path}/{entry}" if rel_path else entry
                    _scan_dir(child_path, child_rel, depth + 1)

    _scan_dir(workspace_path, "", 1)

    # Sort: root first, then alphabetically
    contexts.sort(key=lambda c: (0 if c.path == "." else 1, c.path))
    return contexts


def resolve_docker_image(project_type: Optional[str]) -> str:
    """
    Map a detected project type to its language-appropriate Docker image.

    Parameters
    ----------
    project_type : str | None
        Detected project type from ``detect_project_type``.

    Returns
    -------
    str
        Docker image name. Falls back to ``DOCKER_IMAGE`` if project type is
        unknown or not in the image map.
    """
    from app.core.config import DOCKER_IMAGE, DOCKER_IMAGE_MAP

    if project_type and project_type in DOCKER_IMAGE_MAP:
        return DOCKER_IMAGE_MAP[project_type]
    return DOCKER_IMAGE

