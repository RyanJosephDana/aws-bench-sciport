"""Unit tests for Floci emulator mode helpers."""

import os
from unittest import mock

from aws_bench import emulator


def test_inactive_by_default():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert not emulator.is_active()


def test_active_via_env():
    with mock.patch.dict(os.environ, {"AWS_BENCH_EMULATOR": "floci"}):
        assert emulator.is_active()


def test_static_credentials_shape():
    creds = emulator.static_credentials()
    assert set(creds) == {"AccessKeyId", "SecretAccessKey", "SessionToken"}


def test_account_mapping_single_account():
    assert emulator.account_mapping(["PRIMARY", "SECONDARY"]) == {
        "PRIMARY": emulator.ACCOUNT_ID,
        "SECONDARY": emulator.ACCOUNT_ID,
    }


def test_rewrite_judge_model_bedrock_to_anthropic():
    body = (
        "[judge]\n"
        'judge = "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"\n'
        'prompt_template = "judge_prompt.md"\n'
    )
    out = emulator.rewrite_judge_model(body)
    assert 'judge = "anthropic/claude-sonnet-4-5-20250929"' in out
    assert "bedrock/" not in out


def test_rewrite_judge_model_passthrough():
    body = '[judge]\njudge = "anthropic/claude-sonnet-4-5"\n'
    assert emulator.rewrite_judge_model(body) == body
