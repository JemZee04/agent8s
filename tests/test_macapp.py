import plistlib
import stat
import sys
from pathlib import Path

import pytest

from agent8s.desktop import macapp, service

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="builds a macOS application bundle")


def test_info_plist_describes_a_regular_app():
    info = plistlib.loads(macapp.info_plist())
    assert info["CFBundleExecutable"] == info["CFBundleName"] == "agent8s"
    assert info["CFBundleIdentifier"] == macapp.BUNDLE_ID and info["CFBundlePackageType"] == "APPL"
    assert info["CFBundleIconFile"] == "agent8s" and info["NSHighResolutionCapable"] is True
    assert "LSUIElement" not in info  # it must have a Dock icon and a menu bar, like any app


def test_launcher_starts_the_window_in_app_mode_and_logs_to_a_file():
    script = macapp.launcher_script(Path("/rt/bin/agent8s-desktop"), Path("/logs/app.log"))
    assert script.startswith("#!/bin/bash\n")
    assert 'exec "/rt/bin/agent8s-desktop" --app >> "/logs/app.log" 2>&1' in script  # no terminal to print to


@mac_only
def test_the_bundle_is_built_complete_and_replaced_safely(tmp_path):
    runtime = tmp_path / "runtime" / "bin" / "agent8s-desktop"
    bundle = macapp.install_app(runtime, tmp_path / "Applications", tmp_path / "app.log")
    assert bundle == tmp_path / "Applications" / "agent8s.app"

    launcher = bundle / "Contents" / "MacOS" / "agent8s"
    assert launcher.stat().st_mode & stat.S_IXUSR  # not executable = "the application can't be opened"
    assert str(runtime) in launcher.read_text()
    assert plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())["CFBundleName"] == "agent8s"
    assert (bundle / "Contents" / "Resources" / "agent8s.icns").read_bytes()[:4] == b"icns"
    assert not list((tmp_path / "Applications").glob(".*.new"))  # staging area cleaned up

    (bundle / "stale-file").write_text("from an older install")
    macapp.install_app(runtime, tmp_path / "Applications", tmp_path / "app.log")  # updating in place
    assert not (bundle / "stale-file").exists() and launcher.exists()

    assert macapp.uninstall_app(tmp_path / "Applications") is True
    assert not bundle.exists() and macapp.uninstall_app(tmp_path / "Applications") is False


@mac_only
def test_a_failed_build_leaves_the_working_app_alone(tmp_path, monkeypatch):
    runtime = tmp_path / "rt"
    bundle = macapp.install_app(runtime, tmp_path / "Applications", tmp_path / "app.log")
    marker = bundle / "Contents" / "keep-me"
    marker.write_text("working install")

    def broken(*a, **k):
        raise macapp.AppError("icon tools failed")

    monkeypatch.setattr(macapp, "build_icns", broken)
    with pytest.raises(macapp.AppError):
        macapp.install_app(runtime, tmp_path / "Applications", tmp_path / "app.log")
    assert marker.exists()  # the swap only happens after the new bundle is complete


def test_ensure_running_returns_a_live_service_without_touching_launchd(monkeypatch):
    monkeypatch.setattr(service, "running_service_url", lambda: "http://127.0.0.1:8731/#token=t")
    monkeypatch.setattr(service, "_launchctl", lambda *a: pytest.fail("must not call launchctl"))
    assert service.ensure_running() == "http://127.0.0.1:8731/#token=t"


def test_ensure_running_starts_a_stopped_service(monkeypatch, tmp_path):
    plist = tmp_path / "x.plist"
    plist.write_text("")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    calls, state = [], {"up": False}
    monkeypatch.setattr(service, "running_service_url", lambda: "http://u" if state["up"] else None)

    class Result:
        returncode = 0

    def launchctl(*args):
        calls.append(args[0])
        state["up"] = True
        return Result()

    monkeypatch.setattr(service, "_launchctl", launchctl)
    assert service.ensure_running(timeout=5) == "http://u" and calls == ["kickstart"]


def test_ensure_running_loads_the_job_when_launchd_does_not_know_it(monkeypatch, tmp_path):
    plist = tmp_path / "x.plist"
    plist.write_text("")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    state, calls = {"up": False}, []
    monkeypatch.setattr(service, "running_service_url", lambda: "http://u" if state["up"] else None)

    class Result:
        def __init__(self, code):
            self.returncode = code

    def launchctl(*args):
        calls.append(args[0])
        if args[0] == "kickstart":
            return Result(113)  # "could not find service"
        state["up"] = True
        return Result(0)

    monkeypatch.setattr(service, "_launchctl", launchctl)
    assert service.ensure_running(timeout=5) == "http://u" and calls == ["kickstart", "bootstrap"]


def test_ensure_running_explains_what_to_do_when_nothing_is_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "plist_path", lambda: tmp_path / "missing.plist")
    monkeypatch.setattr(service, "running_service_url", lambda: None)
    with pytest.raises(service.ServiceError, match="--install-app"):
        service.ensure_running()


def test_ensure_running_gives_up_with_a_pointer_to_the_log(monkeypatch, tmp_path):
    plist = tmp_path / "x.plist"
    plist.write_text("")
    monkeypatch.setattr(service, "plist_path", lambda: plist)
    monkeypatch.setattr(service, "running_service_url", lambda: None)

    class Result:
        returncode = 0

    monkeypatch.setattr(service, "_launchctl", lambda *a: Result())
    with pytest.raises(service.ServiceError, match="service.err.log"):
        service.ensure_running(timeout=0.6)
