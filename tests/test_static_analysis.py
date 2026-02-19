"""
Unit Tests — Static Analysis Pipeline
======================================
Tests using synthetic repo fixtures with known issues.
Validates BugReport creation, bug_type assignment, path normalisation, and deduplication.
No formatter used here — this is the detection layer only.
"""
import os
import tempfile
import textwrap
from pathlib import Path

import pytest

from app.services.static_analysis import (
    analyze_repository,
    deduplicate,
    discover_python_files,
    normalize_path,
    run_ast_checks,
)
from app.models.bug_report import BugReport
from app.utils.domain_classifier import classify_domain


# ===================================================================
# Fixtures — create tiny temporary repos
# ===================================================================
@pytest.fixture
def temp_repo(tmp_path):
    """Creates a temp repo with files containing known issues."""
    # File with unused import
    unused_import_file = tmp_path / "unused_import.py"
    unused_import_file.write_text(textwrap.dedent("""\
        import os
        import sys

        def hello():
            return "hello"
    """), encoding="utf-8")

    # File with syntax error (missing colon)
    syntax_error_file = tmp_path / "syntax_error.py"
    syntax_error_file.write_text(textwrap.dedent("""\
        def broken()
            return 1
    """), encoding="utf-8")

    # Clean file (no issues)
    clean_file = tmp_path / "clean.py"
    clean_file.write_text(textwrap.dedent("""\
        def add(a, b):
            return a + b
    """), encoding="utf-8")

    # Nested file
    subdir = tmp_path / "src"
    subdir.mkdir()
    nested = subdir / "nested.py"
    nested.write_text(textwrap.dedent("""\
        import json
        x = 1
    """), encoding="utf-8")

    # Ignored directory
    venv_dir = tmp_path / "venv"
    venv_dir.mkdir()
    ignored_file = venv_dir / "should_ignore.py"
    ignored_file.write_text("import os\n", encoding="utf-8")

    return tmp_path


# ===================================================================
# discover_python_files
# ===================================================================
class TestDiscoverPythonFiles:

    def test_finds_py_files(self, temp_repo):
        files = discover_python_files(str(temp_repo))
        assert "clean.py" in files
        assert "unused_import.py" in files
        assert "syntax_error.py" in files

    def test_finds_nested_files(self, temp_repo):
        files = discover_python_files(str(temp_repo))
        assert "src/nested.py" in files

    def test_ignores_venv(self, temp_repo):
        files = discover_python_files(str(temp_repo))
        for f in files:
            assert "venv" not in f, f"Should have ignored venv, found: {f}"

    def test_returns_sorted(self, temp_repo):
        files = discover_python_files(str(temp_repo))
        assert files == sorted(files)

    def test_empty_dir(self, tmp_path):
        files = discover_python_files(str(tmp_path))
        assert files == []

    def test_uses_forward_slashes(self, temp_repo):
        files = discover_python_files(str(temp_repo))
        for f in files:
            assert "\\" not in f, f"Backslash found in: {f}"


# ===================================================================
# normalize_path
# ===================================================================
class TestNormalizePath:

    def test_absolute_to_relative(self, temp_repo):
        abs_path = str(temp_repo / "clean.py")
        rel = normalize_path(abs_path, str(temp_repo))
        assert rel == "clean.py"

    def test_nested_path(self, temp_repo):
        abs_path = str(temp_repo / "src" / "nested.py")
        rel = normalize_path(abs_path, str(temp_repo))
        assert rel == "src/nested.py"

    def test_forward_slashes(self, temp_repo):
        rel = normalize_path(str(temp_repo / "src" / "nested.py"), str(temp_repo))
        assert "\\" not in rel

    def test_already_relative(self, temp_repo):
        result = normalize_path("src/file.py", str(temp_repo))
        assert "/" in result or result == "src/file.py"


# ===================================================================
# AST Checks (deterministic, no subprocess)
# ===================================================================
class TestRunAstChecks:

    def test_detects_syntax_error(self, temp_repo):
        files = discover_python_files(str(temp_repo))
        reports = run_ast_checks(str(temp_repo), files)
        syntax_bugs = [r for r in reports if r.file_path == "syntax_error.py"]
        assert len(syntax_bugs) >= 1
        assert syntax_bugs[0].bug_type == "SYNTAX"
        assert syntax_bugs[0].tool == "ast"

    def test_no_false_positives_on_clean_file(self, temp_repo):
        reports = run_ast_checks(str(temp_repo), ["clean.py"])
        assert len(reports) == 0

    def test_bug_report_fields_populated(self, temp_repo):
        files = discover_python_files(str(temp_repo))
        reports = run_ast_checks(str(temp_repo), files)
        for r in reports:
            assert r.file_path
            assert r.line_number >= 1
            assert r.bug_type in {"SYNTAX"}
            assert r.sub_type
            assert r.tool == "ast"


# ===================================================================
# Domain Classifier
# ===================================================================
class TestDomainClassifier:

    def test_python_file(self):
        assert classify_domain("src/app.py") == "backend_python"

    def test_javascript_file(self):
        assert classify_domain("src/index.js") == "frontend_js"

    def test_typescript_file(self):
        assert classify_domain("components/App.tsx") == "frontend_js"

    def test_sql_file(self):
        assert classify_domain("db/schema.sql") == "database"

    def test_yaml_file(self):
        assert classify_domain("config/settings.yml") == "config"

    def test_migration_override(self):
        assert classify_domain("migrations/001_init.py") == "database"

    def test_dockerfile_override(self):
        assert classify_domain("docker/Dockerfile") == "config"

    def test_unknown_extension(self):
        assert classify_domain("README.md") == "generic"

    def test_no_extension(self):
        assert classify_domain("Makefile") == "generic"


# ===================================================================
# Deduplication
# ===================================================================
class TestDeduplication:

    def test_removes_exact_duplicates(self):
        r1 = BugReport(bug_type="LINTING", sub_type="unused_import",
                        file_path="a.py", line_number=10, tool="pylint")
        r2 = BugReport(bug_type="LINTING", sub_type="unused_import",
                        file_path="a.py", line_number=10, tool="pyflakes")
        result = deduplicate([r1, r2])
        assert len(result) == 1

    def test_keeps_different_lines(self):
        r1 = BugReport(bug_type="LINTING", sub_type="unused_import",
                        file_path="a.py", line_number=10)
        r2 = BugReport(bug_type="LINTING", sub_type="unused_import",
                        file_path="a.py", line_number=20)
        result = deduplicate([r1, r2])
        assert len(result) == 2

    def test_keeps_different_subtypes_same_line(self):
        r1 = BugReport(bug_type="LINTING", sub_type="unused_import",
                        file_path="a.py", line_number=10)
        r2 = BugReport(bug_type="LINTING", sub_type="unused_variable",
                        file_path="a.py", line_number=10)
        result = deduplicate([r1, r2])
        assert len(result) == 2

    def test_first_occurrence_wins(self):
        r1 = BugReport(bug_type="LINTING", sub_type="unused_import",
                        file_path="a.py", line_number=10, tool="pylint")
        r2 = BugReport(bug_type="LINTING", sub_type="unused_import",
                        file_path="a.py", line_number=10, tool="pyflakes")
        result = deduplicate([r1, r2])
        assert result[0].tool == "pylint"

    def test_empty_list(self):
        assert deduplicate([]) == []


# ===================================================================
# Full Pipeline (analyze_repository)
# ===================================================================
class TestAnalyzeRepository:

    def test_returns_list(self, temp_repo):
        result = analyze_repository(str(temp_repo))
        assert isinstance(result, list)

    def test_detects_syntax_errors(self, temp_repo):
        result = analyze_repository(str(temp_repo))
        syntax_bugs = [r for r in result if r.bug_type == "SYNTAX"]
        # AST check should catch the syntax_error.py file at minimum
        assert len(syntax_bugs) >= 1

    def test_results_are_sorted(self, temp_repo):
        result = analyze_repository(str(temp_repo))
        keys = [(r.file_path, r.line_number) for r in result]
        assert keys == sorted(keys)

    def test_no_duplicates(self, temp_repo):
        result = analyze_repository(str(temp_repo))
        seen = set()
        for r in result:
            key = (r.file_path, r.line_number, r.sub_type)
            assert key not in seen, f"Duplicate found: {key}"
            seen.add(key)

    def test_all_paths_use_forward_slashes(self, temp_repo):
        result = analyze_repository(str(temp_repo))
        for r in result:
            assert "\\" not in r.file_path, f"Backslash in: {r.file_path}"

    def test_empty_repo(self, tmp_path):
        result = analyze_repository(str(tmp_path))
        assert result == []

    def test_all_bug_types_valid(self, temp_repo):
        from app.core.output_formatter import BUG_TYPES
        result = analyze_repository(str(temp_repo))
        for r in result:
            assert r.bug_type in BUG_TYPES, f"Invalid bug_type: {r.bug_type}"

    def test_all_subtypes_have_fix_template(self, temp_repo):
        from app.core.output_formatter import FIX_TEMPLATES
        result = analyze_repository(str(temp_repo))
        for r in result:
            assert r.sub_type in FIX_TEMPLATES.get(r.bug_type, {}), (
                f"sub_type '{r.sub_type}' not in FIX_TEMPLATES['{r.bug_type}']"
            )
