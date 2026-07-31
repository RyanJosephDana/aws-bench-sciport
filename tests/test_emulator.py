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
    assert set(creds) == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}


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


def _make_task(tmp_path, name, introspection):
    task = tmp_path / "scenario" / name
    (task / "tests").mkdir(parents=True)
    (task / "tests" / "test.sh").write_text("#!/bin/bash\nuvx rewardkit /tests\n")
    if introspection:
        (task / "tests" / "ground_truth.json").write_text("{}")
        (task / "tests" / "judge.toml").write_text(
            '[judge]\njudge = "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"\n'
        )
    return task


def test_stage_task_swaps_judge_for_introspection(tmp_path):
    task = _make_task(tmp_path, "diagnose-thing", introspection=True)
    staged = emulator.stage_task_for_emulator(task, tmp_path / "cache")
    assert staged != task
    assert "claude_judge.py" in (staged / "tests" / "test.sh").read_text()
    assert (staged / "tests" / "claude_judge.py").is_file()
    assert "anthropic/claude-sonnet-4-5-20250929" in (staged / "tests" / "judge.toml").read_text()
    # Original dataset untouched.
    assert "rewardkit" in (task / "tests" / "test.sh").read_text()


def test_stage_task_passthrough_for_mutation(tmp_path):
    task = _make_task(tmp_path, "create-thing", introspection=False)
    assert emulator.stage_task_for_emulator(task, tmp_path / "cache") == task


def test_claude_judge_script_is_valid_python():
    import ast

    ast.parse(emulator.CLAUDE_JUDGE_PY)


def test_claude_judge_end_to_end_with_stub_claude(tmp_path):
    import subprocess
    import sys

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "judge.toml").write_text(
        '[judge]\n'
        'judge = "anthropic/claude-sonnet-4-5"\n'
        f'files = ["{tests_dir}/ground_truth.json"]\n'
        'prompt_template = "judge_prompt.md"\n'
        "[[criterion]]\n"
        'name = "answers_equivalent"\n'
        'description = "match?"\n'
        'type = "binary"\n'
    )
    (tests_dir / "judge_prompt.md").write_text("Grade this.\n{criteria}\n")
    (tests_dir / "ground_truth.json").write_text('{"expected_answer": "42"}')
    (tests_dir / "claude_judge.py").write_text(emulator.CLAUDE_JUDGE_PY)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(
        "#!/bin/bash\n"
        "cat > /dev/null\n"
        'echo \'{"result": "{\\"answers_equivalent\\": true}"}\'\n'
    )
    stub.chmod(0o755)

    verifier_dir = tmp_path / "verifier"
    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["AWS_BENCH_VERIFIER_DIR"] = str(verifier_dir)
    proc = subprocess.run(
        [sys.executable, str(tests_dir / "claude_judge.py"), str(tests_dir)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert (verifier_dir / "reward.txt").read_text() == "1.0"


def test_prime_process_env_forces_floci_endpoint(tmp_path):
    env = {
        "AWS_BENCH_EMULATOR": "floci",
        "AWS_BENCH_CLAUDE_TOKEN_FILE": "/nonexistent",
        "AWS_PROFILE": "real-profile",
        "AWS_ACCESS_KEY_ID": "AKIAREALKEY",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        emulator.prime_process_env()
        assert os.environ["AWS_ENDPOINT_URL"] == emulator.DEFAULT_HOST_ENDPOINT
        assert os.environ["AWS_ACCESS_KEY_ID"] == emulator.ACCESS_KEY_ID
        assert "AWS_PROFILE" not in os.environ


def test_chain_assume_role_short_circuits():
    from aws_bench.utils.credentials_provider import CredentialProvider

    with mock.patch.dict(os.environ, {"AWS_BENCH_EMULATOR": "floci"}):
        CredentialProvider.reset()
        try:
            creds = CredentialProvider.get().chain_assume_role(
                account_id="000000000000", session_name="s"
            )
        finally:
            CredentialProvider.reset()
    assert creds == emulator.static_credentials()


def test_bake_trial_prerequisites_appends_once(tmp_path):
    """git is baked into the staged image, not installed per trial.

    Per-trial installation meant every trial in every arm shelled out to a
    package manager at startup, and six containers racing a rate-limited network
    failed together — whole runs came back 0.000 for arms whose tooling had just
    passed preflight.
    """
    from aws_bench.emulator import bake_trial_prerequisites

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-slim\nRUN echo hi\n")

    bake_trial_prerequisites(dockerfile)
    once = dockerfile.read_text()
    assert "FROM python:3.13-slim" in once, "the original image must be preserved"
    assert "install -y --no-install-recommends git" in once

    # Staging re-runs; the layer must not accumulate.
    bake_trial_prerequisites(dockerfile)
    assert dockerfile.read_text() == once


def test_bake_trial_prerequisites_tolerates_a_missing_dockerfile(tmp_path):
    """A task without an environment Dockerfile is not an error."""
    from aws_bench.emulator import bake_trial_prerequisites

    bake_trial_prerequisites(tmp_path / "nope" / "Dockerfile")
