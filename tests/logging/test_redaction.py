"""Tests for ledger env capture and secret redaction helpers."""

from __future__ import annotations

from aws_bench.logging.redaction import REDACTED, record_env, redact_argv, redact_config


class TestRedactArgv:
    def test_redacts_value_after_secret_named_flag(self):
        argv = ["aws-bench", "run", "--registry-token", "supersecretvalue123"]
        assert redact_argv(argv) == ["aws-bench", "run", "--registry-token", REDACTED]

    def test_redacts_equals_form_secret_flag(self):
        argv = ["aws-bench", "--api-token=supersecretvalue123"]
        assert redact_argv(argv) == ["aws-bench", "--api-token=" + REDACTED]

    def test_keeps_ordinary_flags_and_values(self):
        argv = ["aws-bench", "run", "-d", "awsbench@1.0.0"]
        assert redact_argv(argv) == argv

    def test_passes_plain_url_through(self):
        argv = ["aws-bench", "run", "--registry-url", "https://github.com/org/repo"]
        assert redact_argv(argv) == argv

    def test_does_not_redact_long_non_secret_value(self):
        argv = ["aws-bench", "run", "9f3c1ab2d4e5f60718293a4b5c6d7e8f90123456"]
        assert redact_argv(argv) == argv


class TestRedactConfig:
    def test_redacts_secret_named_field(self):
        out = redact_config({"registry_token": "abc123", "n_attempts": 4})
        assert out == {"registry_token": REDACTED, "n_attempts": 4}

    def test_recurses_into_nested_dicts_and_lists(self):
        out = redact_config({"agents": [{"api_key": "x", "name": "kiro"}]})
        assert out == {"agents": [{"api_key": REDACTED, "name": "kiro"}]}

    def test_leaves_plain_url_field_intact(self):
        out = redact_config({"registry_url": "https://github.com/org/repo"})
        assert out == {"registry_url": "https://github.com/org/repo"}

    def test_none_passes_through(self):
        assert redact_config(None) is None


class TestRecordEnv:
    def test_keeps_allowlisted_keys_verbatim(self):
        env = {
            "AWS_PROFILE": "bench",
            "AWS_DEFAULT_PROFILE": "bench-default",
            "AWS_REGION": "us-east-1",
            "AWS_DEFAULT_REGION": "us-west-2",
        }
        assert record_env(env) == env

    def test_drops_unrelated_keys(self):
        out = record_env({"HOME": "/home/x", "SHELL": "/bin/zsh", "AWS_REGION": "us-east-1"})
        assert out == {"AWS_REGION": "us-east-1"}

    def test_drops_aws_credentials(self):
        # Credentials are not on the allowlist, so they never reach disk.
        out = record_env(
            {
                "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
                "AWS_SECRET_ACCESS_KEY": "s",
                "AWS_SESSION_TOKEN": "x",
                "AWS_PROFILE": "bench",
            }
        )
        assert out == {"AWS_PROFILE": "bench"}

    def test_drops_other_secret_named_keys(self):
        out = record_env({"GH_TOKEN": "ghp_realtokenvalue", "DB_PASSWORD": "p"})
        assert out == {}

    def test_empty_when_no_allowlisted_keys_present(self):
        assert record_env({"PATH": "/usr/bin", "TERM": "xterm"}) == {}
