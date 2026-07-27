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


def test_claude_oauth_token_prefers_ambient(tmp_path):
    with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "ambient-token"}):
        assert emulator.claude_oauth_token() == "ambient-token"


def test_claude_oauth_token_reads_file(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("sk-ant-oat0-example\n")
    env = {"AWS_BENCH_CLAUDE_TOKEN_FILE": str(token_file)}
    with mock.patch.dict(os.environ, env, clear=True):
        assert emulator.claude_oauth_token() == "sk-ant-oat0-example"


def test_claude_oauth_token_missing_file():
    env = {"AWS_BENCH_CLAUDE_TOKEN_FILE": "/nonexistent/token"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert emulator.claude_oauth_token() is None


def test_prime_process_env_sets_token(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("sk-ant-oat0-primed")
    env = {
        "AWS_BENCH_EMULATOR": "floci",
        "AWS_BENCH_CLAUDE_TOKEN_FILE": str(token_file),
    }
    with mock.patch.dict(os.environ, env, clear=True):
        emulator.prime_process_env()
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat0-primed"


def test_prime_process_env_noop_when_inactive(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("sk-ant-oat0-ignored")
    env = {"AWS_BENCH_CLAUDE_TOKEN_FILE": str(token_file)}
    with mock.patch.dict(os.environ, env, clear=True):
        emulator.prime_process_env()
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


def test_emulated_test_environment_maps_all_tags():
    from aws_bench.account_management.manager import AccountManager

    with mock.patch.dict(os.environ, {"AWS_BENCH_EMULATOR": "floci"}):
        env = AccountManager().resolve_test_environment(
            "any-ou", required_by_scenario={"ec2-multiregion": {"PRIMARY"}}
        )
    account = env.accounts["ec2-multiregion"]["PRIMARY"]
    assert account.account_id == emulator.ACCOUNT_ID
    assert account.status == "ACTIVE"
    assert env.account_for("ec2-multiregion") == emulator.ACCOUNT_ID
