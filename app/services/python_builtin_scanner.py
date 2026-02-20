"""
Python Built-in Scanner
=======================
PRIMARY error detection for Python repositories using ONLY Python's
built-in standard library — NO external tools (no pylint, pyflakes, mypy).

This module directly inspects every .py file using:
  1. ast.parse()       → SyntaxError, IndentationError, TabError (exact line + col)
  2. py_compile         → SyntaxError from the bytecode compiler (catches edge cases ast misses)
  3. tokenize           → IndentationError, mixed tabs/spaces, encoding errors
  4. importlib.util     → Invalid import detection (checks if top-level imports resolve)

STRICT DETERMINISM CONTRACT:
  - No LLM calls.
  - No external binaries.
  - All detection uses Python stdlib only.
  - Gives precise line numbers.
  - Partial results returned if one file fails.

OUTPUT CONTRACT:
  scan_python_files(repo_path) -> List[BugReport]
  Sorted by (file_path, line_number). Deduplicated.
"""

import ast
import io
import logging
import os
import py_compile
import re
import tokenize
import importlib.util
from pathlib import Path
from typing import List, Optional, Set, Tuple

from app.models.bug_report import BugReport
from app.utils.domain_classifier import classify_domain

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directories to skip
# ---------------------------------------------------------------------------
_SKIP_DIRS: Set[str] = {
    ".venv", "venv", "env", "node_modules", ".git",
    "dist", "build", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".tox", "eggs", ".eggs",
    "site-packages",
}


# ---------------------------------------------------------------------------
# 1. File Discovery
# ---------------------------------------------------------------------------
def _discover_py_files(repo_path: str) -> List[str]:
    """Recursively find all .py files, returning repo-relative paths."""
    repo = Path(repo_path)
    py_files: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if fname.endswith(".py"):
                abs_path = Path(root) / fname
                rel = abs_path.relative_to(repo).as_posix()
                py_files.append(rel)
    return sorted(py_files)


# ---------------------------------------------------------------------------
# 2. ast.parse() — SyntaxError detection
# ---------------------------------------------------------------------------
def _scan_ast(repo_path: str, py_files: List[str]) -> List[BugReport]:
    """
    Parse each file with ast.parse(). Catches SyntaxError, IndentationError,
    TabError with exact line and column numbers.
    """
    reports: list[BugReport] = []
    repo = Path(repo_path)

    for rel_path in py_files:
        abs_path = repo / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            line_no = exc.lineno or 1
            col = exc.offset or 0
            raw_msg = str(exc.msg) if exc.msg else "syntax error"

            # Classify precisely
            bug_type, sub_type = _classify_syntax_error(raw_msg, exc)

            message = f"{raw_msg} (line {line_no}, col {col})"
            reports.append(BugReport(
                bug_type=bug_type,
                sub_type=sub_type,
                file_path=rel_path,
                line_number=max(line_no, 1),
                domain=classify_domain(rel_path),
                tool="ast.parse",
                message=message,
                confidence=1.0,  # stdlib detection = maximum confidence
            ))
            logger.info(
                "[BUILTIN-SCANNER] ast.parse: %s in %s:%d — %s",
                bug_type, rel_path, line_no, raw_msg,
            )
        except Exception as exc:
            logger.debug("[BUILTIN-SCANNER] ast.parse: error reading %s: %s", rel_path, exc)

    return reports


def _classify_syntax_error(msg: str, exc: SyntaxError) -> Tuple[str, str]:
    """Map a SyntaxError message to (bug_type, sub_type)."""
    msg_lower = msg.lower()

    # IndentationError is a subclass of SyntaxError
    if isinstance(exc, IndentationError):
        if "unexpected indent" in msg_lower:
            return ("INDENTATION", "unexpected_indent")
        if "unindent" in msg_lower:
            return ("INDENTATION", "unindent_mismatch")
        return ("INDENTATION", "wrong_indent")

    # TabError is a subclass of IndentationError
    if isinstance(exc, TabError):
        return ("INDENTATION", "mixed_indent")

    # SyntaxError sub-classification
    if "expected ':'" in msg_lower or "missing colon" in msg_lower:
        return ("SYNTAX", "missing_colon")
    if "return" in msg_lower and "outside function" in msg_lower:
        return ("SYNTAX", "return_outside_function")
    if "invalid character" in msg_lower:
        return ("SYNTAX", "invalid_character")
    if "eol while scanning" in msg_lower:
        return ("SYNTAX", "unterminated_string")
    if "eof" in msg_lower:
        return ("SYNTAX", "unexpected_eof")
    if "parenthesis" in msg_lower or "bracket" in msg_lower:
        return ("SYNTAX", "missing_bracket")

    return ("SYNTAX", "invalid_syntax")


# ---------------------------------------------------------------------------
# 3. py_compile — Catches edge cases ast.parse() can miss
# ---------------------------------------------------------------------------
def _scan_py_compile(
    repo_path: str, py_files: List[str], already_found: Set[Tuple[str, int]]
) -> List[BugReport]:
    """
    Compile each file with py_compile. Only reports NEW errors not already
    caught by ast.parse (avoids duplicates).
    """
    reports: list[BugReport] = []
    repo = Path(repo_path)

    for rel_path in py_files:
        abs_path = str(repo / rel_path)
        try:
            py_compile.compile(abs_path, doraise=True)
        except py_compile.PyCompileError as exc:
            # Extract line number from the exception
            line_no = _extract_line_from_pycompile(exc, rel_path)
            key = (rel_path, line_no)
            if key in already_found:
                continue  # already reported by ast.parse

            raw_msg = str(exc)
            bug_type, sub_type = "SYNTAX", "invalid_syntax"
            if "IndentationError" in raw_msg:
                bug_type, sub_type = "INDENTATION", "wrong_indent"
            elif "TabError" in raw_msg:
                bug_type, sub_type = "INDENTATION", "mixed_indent"

            reports.append(BugReport(
                bug_type=bug_type,
                sub_type=sub_type,
                file_path=rel_path,
                line_number=max(line_no, 1),
                domain=classify_domain(rel_path),
                tool="py_compile",
                message=raw_msg[:200],
                confidence=1.0,
            ))
            logger.info(
                "[BUILTIN-SCANNER] py_compile: %s in %s:%d",
                bug_type, rel_path, line_no,
            )
        except Exception:
            pass  # Non-syntax errors are not our concern here

    return reports


def _extract_line_from_pycompile(exc: py_compile.PyCompileError, rel_path: str) -> int:
    """Extract line number from a PyCompileError."""
    # The exc_value attribute contains the original SyntaxError
    if hasattr(exc, 'exc_value') and exc.exc_value and hasattr(exc.exc_value, 'lineno'):
        return exc.exc_value.lineno or 1
    # Fallback: parse from string representation
    m = re.search(r'line\s+(\d+)', str(exc))
    return int(m.group(1)) if m else 1


# ---------------------------------------------------------------------------
# 4. tokenize — Mixed tabs/spaces, encoding issues
# ---------------------------------------------------------------------------
def _scan_tokenize(
    repo_path: str, py_files: List[str], already_found: Set[Tuple[str, int]]
) -> List[BugReport]:
    """
    Tokenize each file to catch indentation issues (mixed tabs/spaces)
    and encoding errors that ast.parse may not report with precise lines.
    """
    reports: list[BugReport] = []
    repo = Path(repo_path)

    for rel_path in py_files:
        abs_path = repo / rel_path
        try:
            source = abs_path.read_bytes()
            tokens = tokenize.tokenize(io.BytesIO(source).readline)
            # Consume all tokens — errors raise during iteration
            for _ in tokens:
                pass
        except tokenize.TokenError as exc:
            line_no = exc.args[1][0] if len(exc.args) > 1 else 1
            key = (rel_path, line_no)
            if key in already_found:
                continue

            reports.append(BugReport(
                bug_type="SYNTAX",
                sub_type="tokenize_error",
                file_path=rel_path,
                line_number=max(line_no, 1),
                domain=classify_domain(rel_path),
                tool="tokenize",
                message=str(exc.args[0])[:200],
                confidence=0.95,
            ))
        except IndentationError as exc:
            line_no = exc.lineno or 1
            key = (rel_path, line_no)
            if key in already_found:
                continue

            reports.append(BugReport(
                bug_type="INDENTATION",
                sub_type="mixed_indent",
                file_path=rel_path,
                line_number=max(line_no, 1),
                domain=classify_domain(rel_path),
                tool="tokenize",
                message=str(exc.msg)[:200] if exc.msg else "indentation error",
                confidence=1.0,
            ))
        except SyntaxError as exc:
            # tokenize can also raise SyntaxError for encoding issues
            line_no = exc.lineno or 1
            key = (rel_path, line_no)
            if key in already_found:
                continue

            reports.append(BugReport(
                bug_type="SYNTAX",
                sub_type="encoding_error",
                file_path=rel_path,
                line_number=max(line_no, 1),
                domain=classify_domain(rel_path),
                tool="tokenize",
                message=str(exc.msg)[:200] if exc.msg else "encoding error",
                confidence=0.9,
            ))
        except Exception:
            pass

    return reports


# ---------------------------------------------------------------------------
# 5. Import Validation — check top-level imports resolve
# ---------------------------------------------------------------------------
def _scan_imports(
    repo_path: str, py_files: List[str], already_found: Set[Tuple[str, int]]
) -> List[BugReport]:
    """
    Parse each file's AST and check that top-level imports can be resolved.
    Uses importlib.util.find_spec() — pure stdlib, no external tools.
    Only reports imports that are clearly broken (not relative, not local).
    """
    reports: list[BugReport] = []
    repo = Path(repo_path)

    # Build set of local module names (files in the repo)
    local_modules: Set[str] = set()
    for rel in py_files:
        parts = Path(rel).with_suffix("").parts
        # e.g., src/utils.py → {"src", "src.utils", "utils"}
        for i in range(len(parts)):
            local_modules.add(".".join(parts[i:]))
        local_modules.add(parts[-1])  # just the filename without extension

    for rel_path in py_files:
        abs_path = repo / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue  # already caught by _scan_ast

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name
                    line_no = node.lineno
                    key = (rel_path, line_no)
                    if key in already_found:
                        continue
                    if _is_broken_import(module_name, local_modules):
                        already_found.add(key)
                        reports.append(BugReport(
                            bug_type="IMPORT",
                            sub_type="missing_import",
                            file_path=rel_path,
                            line_number=line_no,
                            domain=classify_domain(rel_path),
                            tool="importlib",
                            message=f"No module named '{module_name}'",
                            confidence=0.85,
                        ))
                        logger.info(
                            "[BUILTIN-SCANNER] import: broken import '%s' in %s:%d",
                            module_name, rel_path, line_no,
                        )
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                if node.level > 0:
                    continue  # relative import — skip, needs runtime context
                line_no = node.lineno
                key = (rel_path, line_no)
                if key in already_found:
                    continue
                if _is_broken_import(module_name, local_modules):
                    already_found.add(key)
                    reports.append(BugReport(
                        bug_type="IMPORT",
                        sub_type="missing_import",
                        file_path=rel_path,
                        line_number=line_no,
                        domain=classify_domain(rel_path),
                        tool="importlib",
                        message=f"No module named '{module_name}'",
                        confidence=0.85,
                    ))

    return reports


def _is_broken_import(module_name: str, local_modules: Set[str]) -> bool:
    """Check if a module can be found. Returns True if BROKEN."""
    if not module_name:
        return False

    # Skip local/project modules — they might not be importable outside the project
    top_level = module_name.split(".")[0]
    if top_level in local_modules or module_name in local_modules:
        return False

    # Use importlib to check if the module exists
    try:
        spec = importlib.util.find_spec(module_name)
        return spec is None
    except (ModuleNotFoundError, ValueError):
        return True
    except Exception:
        return False  # err on the side of not reporting


# ---------------------------------------------------------------------------
# 6. Unused Import Detection (AST-only, no external tools)
# ---------------------------------------------------------------------------
def _scan_unused_imports(
    repo_path: str, py_files: List[str], already_found: Set[Tuple[str, int]]
) -> List[BugReport]:
    """
    Detect unused imports using pure AST analysis.
    Walks the AST to find import names that are never referenced in the file.
    """
    reports: list[BugReport] = []
    repo = Path(repo_path)

    for rel_path in py_files:
        abs_path = repo / rel_path
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        # Collect all imports
        imports: list[Tuple[str, str, int]] = []  # (as_name, module, lineno)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    imports.append((name, alias.name, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    name = alias.asname or alias.name
                    imports.append((name, f"{node.module}.{alias.name}", node.lineno))

        if not imports:
            continue

        # Collect all Name references in the file (excluding import lines)
        used_names: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                # For things like `os.path` — the `os` part is a Name node
                pass  # Name nodes are already captured

        # Check which imports are unused
        for as_name, full_module, lineno in imports:
            if as_name not in used_names:
                key = (rel_path, lineno)
                if key in already_found:
                    continue
                already_found.add(key)
                reports.append(BugReport(
                    bug_type="LINTING",
                    sub_type="unused_import",
                    file_path=rel_path,
                    line_number=lineno,
                    domain=classify_domain(rel_path),
                    tool="ast_import_check",
                    message=f"'{full_module}' imported but never used",
                    confidence=0.9,
                ))

    return reports


# ===================================================================
# Deduplication
# ===================================================================
def _deduplicate(reports: List[BugReport]) -> List[BugReport]:
    """Remove duplicate BugReports by (file_path, line_number, sub_type)."""
    seen: set[tuple[str, int, str]] = set()
    unique: list[BugReport] = []
    for r in reports:
        key = (r.file_path, r.line_number, r.sub_type)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


# ===================================================================
# PUBLIC ENTRY POINT
# ===================================================================
def scan_python_files(repo_path: str) -> List[BugReport]:
    """
    Run the full Python built-in scanner pipeline on a repository.

    This is the PRIMARY detection layer for Python repositories.
    Uses ONLY Python stdlib — no pip packages, no external binaries.

    Pipeline:
      1. Discover all .py files
      2. ast.parse()   → SyntaxError, IndentationError, TabError
      3. py_compile    → Additional syntax errors
      4. tokenize      → Mixed tabs/spaces, encoding issues
      5. Import check  → Broken imports (importlib.util.find_spec)
      6. Unused import → AST-based unused import detection
      7. Deduplicate + sort

    Returns
    -------
    List[BugReport]
        Deterministic, sorted, deduplicated list of detected bugs.
        All have confidence >= 0.85 (stdlib = trusted).
    """
    logger.info("[BUILTIN-SCANNER] Starting Python built-in scan on: %s", repo_path)

    py_files = _discover_py_files(repo_path)
    logger.info("[BUILTIN-SCANNER] Discovered %d Python files", len(py_files))

    if not py_files:
        logger.warning("[BUILTIN-SCANNER] No Python files found in %s", repo_path)
        return []

    all_reports: list[BugReport] = []

    # Track already-found (file, line) pairs for dedup across scanners
    found: Set[Tuple[str, int]] = set()

    # --- Layer 1: ast.parse (highest confidence) ---
    ast_bugs = _scan_ast(repo_path, py_files)
    for b in ast_bugs:
        found.add((b.file_path, b.line_number))
    all_reports.extend(ast_bugs)

    # --- Layer 2: py_compile (catches edge cases) ---
    compile_bugs = _scan_py_compile(repo_path, py_files, found)
    for b in compile_bugs:
        found.add((b.file_path, b.line_number))
    all_reports.extend(compile_bugs)

    # --- Layer 3: tokenize (indentation / encoding) ---
    token_bugs = _scan_tokenize(repo_path, py_files, found)
    for b in token_bugs:
        found.add((b.file_path, b.line_number))
    all_reports.extend(token_bugs)

    # --- Layer 4: import validation ---
    import_bugs = _scan_imports(repo_path, py_files, found)
    for b in import_bugs:
        found.add((b.file_path, b.line_number))
    all_reports.extend(import_bugs)

    # --- Layer 5: unused imports ---
    unused_bugs = _scan_unused_imports(repo_path, py_files, found)
    all_reports.extend(unused_bugs)

    # Deduplicate and sort
    unique = _deduplicate(all_reports)
    unique.sort(key=lambda r: (r.file_path, r.line_number))

    logger.info(
        "[BUILTIN-SCANNER] Scan complete: %d unique issues across %d files",
        len(unique), len(py_files),
    )
    for b in unique:
        logger.info(
            "[BUILTIN-SCANNER]   %s/%s in %s:%d — %s",
            b.bug_type, b.sub_type, b.file_path, b.line_number, b.message,
        )

    return unique
