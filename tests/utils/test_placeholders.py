"""Tests for placeholder utilities."""

import pytest

from aws_bench.utils.placeholders import (
    PlaceholderAmbiguousError,
    PlaceholderError,
    PlaceholderMissingError,
    PlaceholderOverrideError,
    PlaceholderValidationError,
    build_placeholder_env,
    split_placeholder_key,
    substitute_placeholders,
    update_placeholder_values,
)


def test_substitute_placeholders():
    """Placeholders are replaced with their values."""
    result = substitute_placeholders(
        "Hello {{name}}, id={{id}}", {"PRIMARY": {"name": "Alice", "id": "123"}}
    )
    assert result == "Hello Alice, id=123"


def test_substitute_missing_raises():
    """Missing placeholders raise PlaceholderMissingError by default."""
    with pytest.raises(PlaceholderMissingError):
        substitute_placeholders("{{a}} {{b}}", {"PRIMARY": {"a": "1"}})


def test_substitute_missing_allowed():
    """Missing placeholders are left intact when check is disabled."""
    result = substitute_placeholders(
        "{{a}} {{b}}", {"PRIMARY": {"a": "1"}}, check_missing_placeholders=False
    )
    assert result == "1 {{b}}"


def test_build_placeholder_env():
    """Braces are stripped from keys to build an env dict."""
    assert build_placeholder_env({"{{FOO}}": "bar"}) == {"FOO": "bar"}


def test_qualified_resolves_from_tag():
    out = substitute_placeholders("b={{PRIMARY::Bucket}}", {"PRIMARY": {"Bucket": "b-1"}})
    assert out == "b=b-1"


def test_bare_resolves_against_sole_tag():
    out = substitute_placeholders("v={{VpcId}}", {"PRIMARY": {"VpcId": "vpc-1"}})
    assert out == "v=vpc-1"


def test_bare_against_multi_tag_raises_ambiguous():
    with pytest.raises(PlaceholderAmbiguousError) as ei:
        substitute_placeholders(
            "v={{VpcId}}", {"PRIMARY": {"VpcId": "a"}, "SECONDARY": {"VpcId": "b"}}
        )
    # Message lists the tags and shows the qualified fix.
    msg = str(ei.value)
    assert "PRIMARY" in msg and "SECONDARY" in msg and "::" in msg


def test_qualified_unknown_tag_raises_missing():
    with pytest.raises(PlaceholderMissingError):
        substitute_placeholders("{{NOPE::Bucket}}", {"PRIMARY": {"Bucket": "b"}})


def test_qualified_unknown_name_raises_missing():
    with pytest.raises(PlaceholderMissingError):
        substitute_placeholders("{{PRIMARY::Nope}}", {"PRIMARY": {"Bucket": "b"}})


def test_empty_tag_map_bare_raises_missing():
    with pytest.raises(PlaceholderMissingError):
        substitute_placeholders("{{VpcId}}", {})


def test_check_missing_false_leaves_unresolved_verbatim():
    out = substitute_placeholders(
        "{{PRIMARY::Absent}}", {"PRIMARY": {}}, check_missing_placeholders=False
    )
    assert out == "{{PRIMARY::Absent}}"


def test_update_placeholder_values():
    """New placeholder values are merged into existing ones."""
    result = update_placeholder_values({"PRIMARY": {"a": "1"}}, {"b": "2"})
    assert result == {"PRIMARY": {"a": "1", "b": "2"}}


def test_update_placeholder_values_override_raises():
    """Overriding an existing placeholder raises by default."""
    with pytest.raises(PlaceholderOverrideError):
        update_placeholder_values({"PRIMARY": {"a": "1"}}, {"a": "2"})


def test_update_placeholder_values_override_allowed():
    """Overriding is allowed when raise_on_override is False."""
    result = update_placeholder_values({"PRIMARY": {"a": "1"}}, {"a": "2"}, raise_on_override=False)
    assert result == {"PRIMARY": {"a": "2"}}


def test_update_merges_bare_key_under_sole_tag():
    merged = update_placeholder_values({"PRIMARY": {"A": "1"}}, {"B": "2"})
    assert merged == {"PRIMARY": {"A": "1", "B": "2"}}


def test_update_merges_qualified_key_under_named_tag():
    merged = update_placeholder_values({"PRIMARY": {}, "SECONDARY": {}}, {"SECONDARY::Q": "u"})
    assert merged == {"PRIMARY": {}, "SECONDARY": {"Q": "u"}}


def test_update_bare_key_multi_tag_raises_ambiguous():
    with pytest.raises(PlaceholderAmbiguousError):
        update_placeholder_values({"P": {}, "S": {}}, {"B": "2"})


def test_update_override_raises():
    with pytest.raises(PlaceholderError):
        update_placeholder_values({"PRIMARY": {"A": "1"}}, {"A": "2"})


def test_split_bare_key_returns_none_tag():
    assert split_placeholder_key("VpcId") == (None, "VpcId")


def test_split_qualified_key_splits_on_first_double_colon():
    assert split_placeholder_key("PRIMARY::VpcId") == ("PRIMARY", "VpcId")
    # Split on the FIRST "::" — a later "::" stays in the name half.
    assert split_placeholder_key("A::B::C") == ("A", "B::C")


def test_split_qualified_name_may_contain_dots_and_dashes():
    assert split_placeholder_key("PRIMARY::Stack-Output.Value") == (
        "PRIMARY",
        "Stack-Output.Value",
    )


def test_split_empty_tag_half_raises():
    with pytest.raises(PlaceholderValidationError):
        split_placeholder_key("::VpcId")


def test_split_empty_name_half_raises():
    with pytest.raises(PlaceholderValidationError):
        split_placeholder_key("PRIMARY::")


def test_split_both_empty_raises():
    with pytest.raises(PlaceholderValidationError):
        split_placeholder_key("::")


def test_validation_error_is_placeholder_error_subclass():
    assert issubclass(PlaceholderValidationError, PlaceholderError)


def test_ambiguous_error_is_placeholder_error_subclass():
    assert issubclass(PlaceholderAmbiguousError, PlaceholderError)
