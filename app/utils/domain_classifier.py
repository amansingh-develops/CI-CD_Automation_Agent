"""
Domain Classifier
=================
Classifies file paths into specialist domains for fix routing.

IMPORTANT:  Domain != Bug Type.  These are separate axes.
  - Bug Type determines output format (e.g. LINTING, SYNTAX)
  - Domain determines which specialist agent handles the fix

Supported domains:
  backend_python, frontend_js, database, config, generic
"""
from pathlib import PurePosixPath


# Extension → domain mapping
_EXT_MAP: dict[str, str] = {
    # Python / Backend
    ".py":   "backend_python",
    ".pyw":  "backend_python",
    # JavaScript / Frontend
    ".js":   "frontend_js",
    ".jsx":  "frontend_js",
    ".ts":   "frontend_js",
    ".tsx":  "frontend_js",
    ".vue":  "frontend_js",
    ".svelte": "frontend_js",
    # Database
    ".sql":  "database",
    # Config
    ".yml":  "config",
    ".yaml": "config",
    ".toml": "config",
    ".ini":  "config",
    ".cfg":  "config",
    ".env":  "config",
    ".json": "config",
}

# Path-substring → domain overrides (checked before extension map)
_PATH_OVERRIDES: list[tuple[str, str]] = [
    ("migrations/", "database"),
    ("alembic/",    "database"),
    ("docker",      "config"),
    ("Dockerfile",  "config"),
]


def classify_domain(file_path: str) -> str:
    """
    Classify a repo-relative file path into a specialist domain.

    Parameters
    ----------
    file_path : str
        Repo-relative file path (forward slashes expected).

    Returns
    -------
    str
        One of: backend_python, frontend_js, database, config, generic.
    """
    normalised = file_path.replace("\\", "/")

    # Check path-based overrides first
    for fragment, domain in _PATH_OVERRIDES:
        if fragment in normalised:
            return domain

    # Then check file extension
    ext = PurePosixPath(normalised).suffix.lower()
    return _EXT_MAP.get(ext, "generic")
