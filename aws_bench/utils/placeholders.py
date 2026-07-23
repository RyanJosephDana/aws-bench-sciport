"""Placeholder substitution utilities for task instructions and verifier env."""

import re

from aws_bench.exceptions import AWSBenchError
from aws_bench.logging.logger import get_logger

logger = get_logger(__name__)


class PlaceholderError(AWSBenchError):
    """Raised when placeholder substitution fails."""


class PlaceholderOverrideError(PlaceholderError):
    """Raised when a placeholder update would override an existing value."""


class PlaceholderMissingError(PlaceholderError):
    """Raised when a well-formed placeholder has no value in the tag map."""


class PlaceholderValidationError(PlaceholderError):
    """Raised when a placeholder key is malformed (empty key or empty tag/name half).

    Distinct from ``PlaceholderMissingError``: the key is not merely absent from the
    map, it is not a valid ``name`` / ``TAG::name`` reference in the first place.
    """


class PlaceholderAmbiguousError(PlaceholderError):
    """Raised when a bare placeholder is used against a multi-account tag map.

    The value is not missing — it exists under one or more account tags — but the
    placeholder did not say which. The fix is to qualify it as ``{{TAG::Export}}``.
    """


_TAG_SEPARATOR = "::"
_PLACEHOLDER_RE = re.compile(r"\{\{([^}]+)\}\}")


def split_placeholder_key(key: str) -> tuple[str | None, str]:
    """Split a placeholder key into ``(tag, name)``.

    ``TAG::name`` -> ``(TAG, name)`` (split on the first ``::``); a key with no
    ``::`` -> ``(None, name)``. An empty key, empty tag half, or empty name half is
    malformed and raises ``PlaceholderValidationError``.
    """
    if _TAG_SEPARATOR not in key:
        if not key:
            raise PlaceholderValidationError("Empty placeholder key")
        return None, key
    tag, name = key.split(_TAG_SEPARATOR, 1)
    if not tag or not name:
        raise PlaceholderValidationError(f"Malformed placeholder key {key!r}")
    return tag, name


def resolve_placeholder(tag_map: dict[str, dict[str, str]], key: str) -> str | None:
    """Resolve one placeholder key against the tag-map, or return None if absent.

    ``TAG::name`` resolves ``tag_map[TAG][name]``. A bare ``name`` resolves against
    the sole tag when exactly one exists; against a >1-tag map it is ambiguous and
    raises ``PlaceholderAmbiguousError``. A None return means the key is genuinely
    absent (unknown tag or unknown name) — the caller decides whether that raises.
    """
    tag, name = split_placeholder_key(key)
    if tag is not None:
        return tag_map.get(tag, {}).get(name)
    if len(tag_map) > 1:
        raise PlaceholderAmbiguousError(
            f"Bare placeholder {{{{{name}}}}} is ambiguous: this trial spans "
            f"accounts {sorted(tag_map)}; qualify it with a tag, "
            f"e.g. {{{{{sorted(tag_map)[0]}::{name}}}}}."
        )
    if not tag_map:
        return None
    (sole_values,) = tag_map.values()
    return sole_values.get(name)


def substitute_placeholders(
    template: str,
    tag_map: dict[str, dict[str, str]] | None = None,
    check_missing_placeholders: bool = True,
) -> str:
    """Substitute ``{{placeholder}}`` patterns in ``template`` from ``tag_map``.

    Placeholders are ``{{TAG::Export}}`` (qualified) or ``{{Export}}`` (bare, resolved
    against the sole account tag). By default an unresolved placeholder raises
    ``PlaceholderMissingError``; a bare placeholder against a multi-account map raises
    ``PlaceholderAmbiguousError`` regardless of ``check_missing_placeholders``.
    """
    if tag_map is None:
        tag_map = {}

    keys = _PLACEHOLDER_RE.findall(template)

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key in keys:
        value = resolve_placeholder(tag_map, key)
        if value is None:
            missing.append(key)
        else:
            resolved[key] = value

    if check_missing_placeholders and missing:
        logger.debug(f"Missing values for placeholders: {missing}")
        raise PlaceholderMissingError(f"No values provided for placeholders: {', '.join(missing)}")

    def replace(match):
        return str(resolved.get(match.group(1), match.group(0)))

    return _PLACEHOLDER_RE.sub(replace, template)


def build_placeholder_env(variables: dict[str, str]) -> dict[str, str]:
    """Convert {{placeholder}} keys to plain env var names."""
    return {k.strip("{}"): v for k, v in variables.items()}


def update_placeholder_values(
    tag_map: dict[str, dict[str, str]],
    updates: dict[str, str],
    raise_on_override: bool = True,
) -> dict[str, dict[str, str]]:
    """Fold flat ``updates`` (placeholder-key -> value) into a copy of ``tag_map``.

    Each update key is a placeholder key: ``TAG::name`` targets that tag; a bare
    ``name`` targets the sole tag, and is ambiguous (raises) against a >1-tag map.
    An update that would overwrite an existing ``(tag, name)`` raises
    ``PlaceholderOverrideError`` unless ``raise_on_override`` is False.
    """
    merged = {tag: dict(values) for tag, values in tag_map.items()}
    for key, value in updates.items():
        tag, name = split_placeholder_key(key)
        if tag is None:
            if len(merged) > 1:
                raise PlaceholderAmbiguousError(
                    f"Bare placeholder key {name!r} is ambiguous across "
                    f"accounts {sorted(merged)}; qualify it as TAG::{name}."
                )
            tag = next(iter(merged)) if merged else ""
        existing = merged.setdefault(tag, {})
        if name in existing and raise_on_override:
            where = f"account tag {tag!r}" if tag else "the account exports"
            raise PlaceholderOverrideError(f"Placeholder '{key}' already has a value in {where}.")
        existing[name] = value
    return merged
