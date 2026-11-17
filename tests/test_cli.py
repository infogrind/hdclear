"""End-to-end CLI runs against a file pretending to be a USB disk.

Everything that would touch the real system (root check, sysfs, SMART,
partitioning, mkfs) is stubbed; the surface test itself runs for real.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_disk

from hdclear import cli, contents, disks, formatter, smart, surface, ui
from hdclear.disks import Mount, SystemState

SIZE = 2 * 1024 * 1024


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Stub the system; return a dict tests can tweak and inspect."""
    img = tmp_path / "disk.img"
    img.write_bytes(b"\0" * SIZE)
    disk = make_disk(path=img, size=SIZE, partitions=[])
    calls: list[str] = []
    state = {
        "disk": disk,
        "calls": calls,
        "mounts": SystemState([], set(), set()),
        "smart": [smart.SmartInfo(available=True, serial="ZCT0AB12", passed=True)],
        "answer": "ab12",
        "tty": True,
    }

    monkeypatch.setattr(cli, "check_environment", lambda: None)
    monkeypatch.setattr(disks, "resolve_device", lambda arg: "sdq")
    monkeypatch.setattr(disks, "read_disk", lambda name: state["disk"])
    monkeypatch.setattr(disks, "read_system_state", lambda: state["mounts"])
    monkeypatch.setattr(contents, "inspect_disk", lambda d, m: [])
    monkeypatch.setattr(
        smart, "query", lambda dev: state["smart"][0] if len(state["smart"]) == 1 else state["smart"].pop(0)
    )
    monkeypatch.setattr(
        formatter, "required_tools", lambda: {"mkfs.ext4": "x", "e2fsck": "x", "umount": "x", "partitioner": "x"}
    )
    monkeypatch.setattr(formatter, "unmount", lambda mounts: calls.append("unmount"))
    monkeypatch.setattr(formatter, "partition", lambda d: calls.append("partition"))
    monkeypatch.setattr(formatter, "reread_partitions", lambda d: Path("/dev/sdq1"))
    monkeypatch.setattr(formatter, "make_ext4", lambda part, label: calls.append(f"mkfs {label}"))
    monkeypatch.setattr(formatter, "check_ext4", lambda part: calls.append("fsck") or "clean")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: state["tty"], raising=False)
    monkeypatch.setattr(ui.console, "input", lambda prompt="": state["answer"])
    # Small chunks so the 2 MiB "disk" spans several of them.
    real_init = surface.SurfaceTest.__init__

    def small_chunks(self, path, size, **kw):
        kw.update(chunk=64 * 1024, direct=False)
        real_init(self, path, size, **kw)

    monkeypatch.setattr(surface.SurfaceTest, "__init__", small_chunks)
    return state


def run(tmp_path, *extra):
    return cli.main(["/dev/sdq", "--log-dir", str(tmp_path / "logs"), *extra])


def test_full_run_passes_and_formats(env, tmp_path, capsys):
    code = run(tmp_path, "-L", "backup1")

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "PASSED" in out
    assert env["calls"] == ["partition", "mkfs backup1", "fsck"]
    log = next((tmp_path / "logs").iterdir()).read_text()
    assert "RESULT: PASSED" in log
    assert "verify: 100%" in log


def test_wrong_confirmation_changes_nothing(env, tmp_path, capsys):
    env["answer"] = "sdq"
    before = env["disk"].path.read_bytes()

    assert run(tmp_path) == cli.EXIT_ERROR
    assert env["disk"].path.read_bytes() == before
    assert env["calls"] == []
    assert "nothing was changed" in capsys.readouterr().out


def test_dry_run_changes_nothing(env, tmp_path, capsys):
    before = env["disk"].path.read_bytes()

    assert run(tmp_path, "--dry-run") == cli.EXIT_OK
    assert env["disk"].path.read_bytes() == before
    assert "Dry run" in capsys.readouterr().out


def test_non_interactive_is_refused(env, tmp_path, capsys):
    env["tty"] = False
    assert run(tmp_path) == cli.EXIT_ERROR
    assert "interactive terminal" in capsys.readouterr().out


def test_blocked_disk_is_refused(env, tmp_path, capsys):
    env["mounts"] = SystemState([Mount("sdq", "/volume1", "btrfs")], set(), set())

    assert run(tmp_path) == cli.EXIT_ERROR
    out = capsys.readouterr().out
    assert "system path" in out
    assert "refusing" in out


def test_usb_mount_is_unmounted_first(env, tmp_path):
    env["mounts"] = SystemState([Mount("sdq", "/volumeUSB1/usbshare", "ext4")], set(), set())

    assert run(tmp_path) == cli.EXIT_OK
    assert env["calls"][0] == "unmount"


def test_smart_regression_fails_and_skips_format(env, tmp_path, capsys):
    before = smart.SmartInfo(available=True, serial="ZCT0AB12", passed=True, attributes={5: ("r", 0)})
    after = smart.SmartInfo(available=True, serial="ZCT0AB12", passed=True, attributes={5: ("r", 24)})
    env["smart"] = [before, after]

    assert run(tmp_path) == cli.EXIT_DISK_FAILED
    out = capsys.readouterr().out
    assert "FAILED" in out and "NOT formatted" in out
    assert env["calls"] == []


def test_bad_sectors_fail(env, tmp_path, monkeypatch, capsys):
    real_read_pass = surface.SurfaceTest.read_pass

    def corrupting_read_pass(self):
        with open(self.path, "r+b") as f:
            f.seek(100_000)
            f.write(b"oops")
        return real_read_pass(self)

    monkeypatch.setattr(surface.SurfaceTest, "read_pass", corrupting_read_pass)

    assert run(tmp_path) == cli.EXIT_DISK_FAILED
    out = capsys.readouterr().out
    assert "data mismatch" in out
    assert env["calls"] == []


@pytest.mark.parametrize(
    ("serial", "expected"),
    [("ZCT0AB12", ("AB12", "last 4 characters of the serial number")),
     ("WD-WX12-3", ("X123", "last 4 characters of the serial number")),
     (None, ("sdq", "device name (sdq)")),
     ("AB", ("sdq", "device name (sdq)"))],
)
def test_confirmation_code(serial, expected):
    assert cli.confirmation_code(serial, "sdq") == expected


def test_list_mode_shows_disks(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda: [make_disk()])
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)

    assert cli.main([]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "/dev/sdq" in out and "8.0 TB" in out


def test_default_log_dir_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert cli.default_log_dir() == tmp_path / "hdclear"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/path")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert cli.default_log_dir() == tmp_path / ".local/state/hdclear"
