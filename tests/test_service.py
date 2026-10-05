import plistlib
from pathlib import Path

from agent8s.desktop import service


def test_plist_runs_headless_on_the_fixed_port_and_restarts_only_after_crashes():
    data = plistlib.loads(service.build_plist(Path("/x/bin/agent8s-desktop"), {"PATH": "/a"}))
    assert data["ProgramArguments"] == ["/x/bin/agent8s-desktop", "--no-window", "--port", str(service.PORT)]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}  # a clean stop must stay stopped
    assert data["LimitLoadToSessionType"] == "Aqua"  # permission prompts need the GUI session
    assert data["EnvironmentVariables"] == {"PATH": "/a"}


def test_service_never_touches_the_repository_directories():
    env = service.service_environment({"PATH": "/custom/bin:/usr/bin", "AGENT8S_RELAY_URL": "https://x/agent8s",
                                       "AGENT8S_DATA_DIR": "/repo/data", "TELEGRAM_BOT_TOKEN": "secret"})
    # data and worktrees live under Application Support, whatever the interactive env says
    assert env["AGENT8S_DATA_DIR"] == str(service.DATA_DIR)
    assert "Application Support" in env["AGENT8S_WORKTREE_DIR"]
    assert env["AGENT8S_RELAY_URL"] == "https://x/agent8s"
    assert "TELEGRAM_BOT_TOKEN" not in env  # only whitelisted settings are carried over
    parts = env["PATH"].split(":")
    assert parts[0] == "/custom/bin" and "/opt/homebrew/bin" in parts and parts.count("/usr/bin") == 1


def test_running_service_url_ignores_stale_or_missing_info(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    assert service.running_service_url() is None
    (tmp_path / "desktop.json").write_text('{"port": 1, "token": "t", "pid": 999999999}')
    assert service.running_service_url() is None  # pid not alive
    (tmp_path / "desktop.json").write_text("garbage")
    assert service.running_service_url() is None
