import asyncio
import os
import stat
from pathlib import Path

import pytest

from agent8s.desktop import agent_auth


def fake_cli(directory: Path, name: str, output: str, code: int = 0, sleep: float = 0):
    path = directory / name
    path.write_text(f"#!/bin/bash\nsleep {sleep}\ncat <<'EOF'\n{output}\nEOF\nexit {code}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", f"{directory}:/usr/bin:/bin")
    agent_auth.forget_cached()
    return directory


async def test_claude_signed_in_and_out(bin_dir):
    fake_cli(bin_dir, "claude", '{"loggedIn": true, "authMethod": "oauth"}')
    assert (await agent_auth.check_auth("claude"))["ok"] is True
    fake_cli(bin_dir, "claude", '{"loggedIn": false, "authMethod": "none"}')
    assert (await agent_auth.check_auth("claude", refresh=True))["ok"] is False


async def test_codex_signed_in_and_out(bin_dir):
    fake_cli(bin_dir, "codex", "Logged in using ChatGPT")
    assert (await agent_auth.check_auth("codex"))["ok"] is True
    fake_cli(bin_dir, "codex", "Not logged in", code=1)
    assert (await agent_auth.check_auth("codex", refresh=True))["ok"] is False


async def test_unknown_when_the_tool_is_missing_or_speaks_nonsense(bin_dir, tmp_path, monkeypatch):
    fake_cli(bin_dir, "claude", "this is not json")
    assert (await agent_auth.check_auth("claude"))["ok"] is None  # never claim "signed out" without evidence
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    agent_auth.forget_cached()
    result = await agent_auth.check_auth("codex")
    assert result["ok"] is None and result["login"] == "codex login" and result["label"] == "Codex"


async def test_results_are_cached_briefly(bin_dir):
    fake_cli(bin_dir, "claude", '{"loggedIn": true}')
    assert (await agent_auth.check_auth("claude"))["ok"] is True
    fake_cli(bin_dir, "claude", '{"loggedIn": false}')
    assert (await agent_auth.check_auth("claude"))["ok"] is True  # served from the cache, no new process
    assert (await agent_auth.check_auth("claude", refresh=True))["ok"] is False


def test_the_real_error_from_the_field_gets_an_actionable_explanation():
    raw = "Failed to authenticate: OAuth session expired and could not be refreshed"
    out = agent_auth.explain_error("claude", raw)
    assert out.startswith(raw) and "claude auth login" in out and "«Войти»" in out
    assert "codex login" in agent_auth.explain_error("codex", "Not logged in. Please run codex login")


def test_other_errors_pass_through_untouched():
    for agent, text in (("claude", "API Error: 500 overloaded"), ("claude", "claude exited with code 1: boom"),
                        ("codex", "model requires a newer version"), ("claude", None), ("gemini", "Not logged in")):
        assert agent_auth.explain_error(agent, text) == text


def test_the_login_script_uses_the_absolute_path_and_the_right_command(bin_dir):
    fake_cli(bin_dir, "claude", "x")
    script = agent_auth.login_script("claude")
    assert script.startswith("#!/bin/bash\n") and f'"{bin_dir}/claude" auth login' in script
    assert "TOKEN" not in script and "secret" not in script.lower()
    assert 'codex" login' in agent_auth.login_script("codex") or "codex login" in agent_auth.login_script("codex")


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="opens Terminal.app")
def test_the_login_button_opens_terminal_with_an_executable_script(bin_dir, tmp_path):
    opened = []
    script = agent_auth.open_login_terminal("claude", tmp_path / "data", opener=opened.append)
    assert script.stat().st_mode & stat.S_IXUSR  # `open -a Terminal x.command` needs it executable
    assert opened == [["open", "-a", "Terminal", str(script)]]  # no Automation permission needed, unlike osascript
