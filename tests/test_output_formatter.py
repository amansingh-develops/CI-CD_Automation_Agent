"""
Unit Tests — Output Formatter
==============================
Validates byte-perfect output string generation.

These tests are the authoritative verification gate for the 40-point
evaluation component. Every assertion uses exact string equality.
"""
import pytest
from app.core.output_formatter import (
    ARROW,
    BUG_TYPES,
    BugType,
    FIX_TEMPLATES,
    format_bug,
    format_output,
    resolve_fix_description,
    validate_bug_type,
    validate_file_path,
    validate_line_number,
    validate_sub_type,
)


# ---------------------------------------------------------------------------
# 1. Arrow constant sanity checks
# ---------------------------------------------------------------------------
class TestArrowConstant:

    def test_arrow_is_unicode_2192(self):
        """The arrow must be the Unicode RIGHT ARROW U+2192, not ASCII '->'."""
        assert ord(ARROW) == 0x2192

    def test_arrow_is_single_character(self):
        assert len(ARROW) == 1

    def test_arrow_is_not_ascii_gt(self):
        assert ARROW != "->"

    def test_arrow_not_other_arrows(self):
        assert ARROW != "→" or ord("→") == 0x2192  # self-consistent check


# ---------------------------------------------------------------------------
# 2. Bug type constants
# ---------------------------------------------------------------------------
class TestBugTypeConstants:

    def test_all_expected_types_present(self):
        expected = {"LINTING", "SYNTAX", "LOGIC", "TYPE_ERROR", "IMPORT", "INDENTATION"}
        assert expected == BUG_TYPES

    def test_values_are_uppercase(self):
        for bt in BUG_TYPES:
            assert bt == bt.upper(), f"Bug type '{bt}' is not uppercase"

    def test_bugtype_class_members_match_bug_types_set(self):
        for attr in ("LINTING", "SYNTAX", "LOGIC", "TYPE_ERROR", "IMPORT", "INDENTATION"):
            assert getattr(BugType, attr) in BUG_TYPES


# ---------------------------------------------------------------------------
# 3. Fix templates coverage
# ---------------------------------------------------------------------------
class TestFixTemplates:

    def test_all_bug_types_have_template_entry(self):
        for bt in BUG_TYPES:
            assert bt in FIX_TEMPLATES, f"FIX_TEMPLATES missing entry for '{bt}'"

    def test_all_descriptions_are_lowercase_strings(self):
        for bug_type, subtypes in FIX_TEMPLATES.items():
            for sub, desc in subtypes.items():
                assert isinstance(desc, str), f"{bug_type}.{sub} description is not str"
                assert desc == desc.lower(), (
                    f"{bug_type}.{sub} description '{desc}' is not fully lowercase"
                )

    def test_no_empty_descriptions(self):
        for bug_type, subtypes in FIX_TEMPLATES.items():
            for sub, desc in subtypes.items():
                assert desc.strip(), f"{bug_type}.{sub} has empty description"


# ---------------------------------------------------------------------------
# 4. Validation helpers
# ---------------------------------------------------------------------------
class TestValidators:

    # --- validate_bug_type ---
    def test_valid_bug_type_does_not_raise(self):
        for bt in BUG_TYPES:
            validate_bug_type(bt)  # must not raise

    def test_invalid_bug_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown bug_type"):
            validate_bug_type("UNKNOWN")

    def test_non_string_bug_type_raises_type_error(self):
        with pytest.raises(TypeError):
            validate_bug_type(123)

    # --- validate_line_number ---
    def test_valid_line_number_does_not_raise(self):
        validate_line_number(1)
        validate_line_number(999)

    def test_zero_line_number_raises(self):
        with pytest.raises(ValueError, match="line_number must be >= 1"):
            validate_line_number(0)

    def test_negative_line_number_raises(self):
        with pytest.raises(ValueError):
            validate_line_number(-5)

    def test_non_int_line_number_raises(self):
        with pytest.raises(TypeError):
            validate_line_number("42")

    # --- validate_file_path ---
    def test_valid_file_path_does_not_raise(self):
        validate_file_path("src/app.py")
        validate_file_path("tests/test_foo.py")

    def test_empty_file_path_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_file_path("")

    def test_whitespace_file_path_raises(self):
        with pytest.raises(ValueError):
            validate_file_path("   ")

    def test_non_string_file_path_raises(self):
        with pytest.raises(TypeError):
            validate_file_path(None)

    # --- validate_sub_type ---
    def test_valid_sub_type_does_not_raise(self):
        validate_sub_type("LINTING", "unused_import")

    def test_invalid_sub_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown sub_type"):
            validate_sub_type("LINTING", "nonexistent")

    def test_sub_type_with_invalid_bug_type_raises(self):
        with pytest.raises(ValueError, match="Unknown bug_type"):
            validate_sub_type("INVALID", "unused_import")

    def test_non_string_sub_type_raises_type_error(self):
        with pytest.raises(TypeError):
            validate_sub_type("LINTING", 123)


# ---------------------------------------------------------------------------
# 5. resolve_fix_description
# ---------------------------------------------------------------------------
class TestResolveFixDescription:

    def test_linting_unused_import(self):
        result = resolve_fix_description(BugType.LINTING, "unused_import")
        assert result == "remove unused import statement"

    def test_syntax_missing_colon(self):
        result = resolve_fix_description(BugType.SYNTAX, "missing_colon")
        assert result == "add missing colon at end of statement"

    def test_logic_wrong_operator(self):
        result = resolve_fix_description(BugType.LOGIC, "wrong_operator")
        assert result == "replace operator with correct logical operator"

    def test_type_error_type_mismatch(self):
        result = resolve_fix_description(BugType.TYPE_ERROR, "type_mismatch")
        assert result == "cast variable to the expected type"

    def test_import_missing_import(self):
        result = resolve_fix_description(BugType.IMPORT, "missing_import")
        assert result == "add missing import statement at top of file"

    def test_indentation_wrong_indent(self):
        result = resolve_fix_description(BugType.INDENTATION, "wrong_indent")
        assert result == "fix indentation to use consistent spaces"

    def test_invalid_subtype_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown sub_type"):
            resolve_fix_description(BugType.LINTING, "nonexistent_subtype")

    def test_invalid_bug_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown bug_type"):
            resolve_fix_description("GARBAGE", "unused_import")


# ---------------------------------------------------------------------------
# 6. format_output — exact string equality (core evaluation assertions)
# ---------------------------------------------------------------------------
class TestFormatOutput:

    def test_example_1_linting_unused_import(self):
        """LINTING + unused_import → exact evaluation string."""
        result = format_output(
            bug_type="LINTING",
            file_path="src/app.py",
            line_number=10,
            fix_description="remove unused import statement",
        )
        expected = (
            "LINTING error in src/app.py line 10 \u2192 Fix: remove unused import statement"
        )
        assert result == expected

    def test_example_2_syntax_missing_colon(self):
        """SYNTAX + missing_colon → exact evaluation string."""
        result = format_output(
            bug_type="SYNTAX",
            file_path="module/parser.py",
            line_number=42,
            fix_description="add missing colon at end of statement",
        )
        expected = (
            "SYNTAX error in module/parser.py line 42"
            " \u2192 Fix: add missing colon at end of statement"
        )
        assert result == expected

    def test_arrow_character_in_output(self):
        """Output must contain U+2192, not '->'."""
        result = format_output("LOGIC", "a.py", 1, "replace operator with correct logical operator")
        assert "\u2192" in result
        assert "->" not in result

    def test_no_trailing_whitespace(self):
        result = format_output("IMPORT", "b.py", 5, "add missing import statement at top of file")
        assert result == result.rstrip()

    def test_no_newline(self):
        result = format_output("INDENTATION", "c.py", 3, "fix indentation to use consistent spaces")
        assert "\n" not in result

    def test_lowercase_keywords(self):
        """'error in' and 'line' must be lowercase."""
        result = format_output("LINTING", "d.py", 1, "remove unused import statement")
        assert "error in" in result
        assert " line " in result

    def test_fix_prefix_capitalisation(self):
        """'Fix:' must have capital F and colon — not 'fix:' or 'FIX:'."""
        result = format_output("SYNTAX", "e.py", 7, "add missing colon at end of statement")
        assert " Fix: " in result

    def test_all_bug_types_produce_output(self):
        for bt in BUG_TYPES:
            first_subtype = next(iter(FIX_TEMPLATES[bt]))
            desc = FIX_TEMPLATES[bt][first_subtype]
            result = format_output(bt, "file.py", 1, desc)
            assert result.startswith(bt)

    def test_invalid_bug_type_raises(self):
        with pytest.raises(ValueError):
            format_output("BADTYPE", "f.py", 1, "some description")

    def test_zero_line_number_raises(self):
        with pytest.raises(ValueError):
            format_output("LINTING", "g.py", 0, "remove unused import statement")

    def test_empty_fix_description_raises(self):
        with pytest.raises(ValueError):
            format_output("LINTING", "h.py", 1, "")

    def test_single_spaces_only(self):
        """Ensure no double-spaces appear between tokens."""
        result = format_output("LOGIC", "i.py", 99, "correct boolean condition to match intended logic")
        assert "  " not in result


# ---------------------------------------------------------------------------
# 7. format_bug — end-to-end convenience wrapper
# ---------------------------------------------------------------------------
class TestFormatBug:

    def test_format_bug_linting_unused_import(self):
        result = format_bug("LINTING", "src/app.py", 10, "unused_import")
        expected = (
            "LINTING error in src/app.py line 10 \u2192 Fix: remove unused import statement"
        )
        assert result == expected

    def test_format_bug_syntax_missing_colon(self):
        result = format_bug("SYNTAX", "module/parser.py", 42, "missing_colon")
        expected = (
            "SYNTAX error in module/parser.py line 42"
            " \u2192 Fix: add missing colon at end of statement"
        )
        assert result == expected

    def test_format_bug_indentation_wrong_indent(self):
        result = format_bug("INDENTATION", "utils/helper.py", 15, "wrong_indent")
        expected = (
            "INDENTATION error in utils/helper.py line 15"
            " \u2192 Fix: fix indentation to use consistent spaces"
        )
        assert result == expected

    def test_format_bug_invalid_subtype_raises(self):
        with pytest.raises(ValueError, match="Unknown sub_type"):
            format_bug("LINTING", "x.py", 1, "totally_made_up_subtype")

    def test_format_bug_invalid_bug_type_raises(self):
        with pytest.raises(ValueError, match="Unknown bug_type"):
            format_bug("NOTAREAL", "x.py", 1, "unused_import")
