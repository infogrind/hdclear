import os
from pathlib import Path

from hdclear import contents, tools
from hdclear.disks import Mount


def populate(root: Path) -> None:
    (root / "Photos" / "2019").mkdir(parents=True)
    (root / "Photos" / "2019" / "a.jpg").write_bytes(b"x" * 1000)
    (root / "Documents").mkdir()
    (root / "Documents" / "taxes.pdf").write_bytes(b"x" * 500)
    (root / "readme.txt").write_bytes(b"hi")
    os.utime(root / "Photos" / "2019" / "a.jpg", (1_500_000_000, 1_500_000_000))
    os.utime(root / "Documents" / "taxes.pdf", (1_700_000_000, 1_700_000_000))
    os.utime(root / "readme.txt", (1_600_000_000, 1_600_000_000))


def test_scan_tree_counts_and_finds_newest(tmp_path):
    populate(tmp_path)

    result = contents.scan_tree(tmp_path)

    assert (result.files, result.dirs, result.bytes) == (3, 3, 1502)
    assert result.newest_path == os.path.join("Documents", "taxes.pdf")
    assert result.complete


def test_scan_tree_stops_early(tmp_path):
    populate(tmp_path)
    assert not contents.scan_tree(tmp_path, max_entries=2).complete


def test_list_top_puts_folders_first(tmp_path):
    populate(tmp_path)

    top, more = contents.list_top(tmp_path)

    assert [(e.name, e.is_dir) for e in top] == [
        ("Documents", True),
        ("Photos", True),
        ("readme.txt", False),
    ]
    assert more == 0


def test_inspect_volume_uses_existing_mount(tmp_path, monkeypatch):
    populate(tmp_path)
    monkeypatch.setattr(tools, "find_tool", lambda name: None)  # no blkid

    info = contents.inspect_volume(
        Path("/dev/sdq1"), 10**12, [Mount("sdq1", str(tmp_path), "ext4")]
    )

    assert info.fstype == "ext4"
    assert info.mountpoint == str(tmp_path)
    assert not info.temp_mounted
    assert info.total and info.used is not None
    assert info.scan.files == 3


def test_inspect_volume_raid_member_is_not_mounted(monkeypatch):
    monkeypatch.setattr(contents, "probe_fs", lambda dev: {"TYPE": "linux_raid_member"})

    info = contents.inspect_volume(Path("/dev/sdq5"), 10**12, [])

    assert "RAID member" in info.note
    assert info.mountpoint is None
