"""Builders for fake sysfs trees and fake disks.

Each builder takes keyword overrides on top of realistic defaults, so tests
only spell out what they care about. Serials and models are made up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hdclear.disks import Disk, Partition


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def add_sys_disk(
    sys_root: Path,
    name: str = "sdq",
    *,
    transport: str = "usb",
    sectors: int = 15628053168,  # 8 TB
    vendor: str = "ST8000DM",
    model: str = "004-2CX188",
    usb_serial: str | None = "QP0000FAKE01",
    partitions: dict[str, int] | None = None,
    holders: list[str] | None = None,
    part_holders: dict[str, list[str]] | None = None,
) -> Path:
    """Create /sys/devices/... for one disk and the /sys/block/<name> symlink to it."""
    if transport == "usb":
        usb_dev = sys_root / "devices/pci0000:00/0000:00:14.0/usb2/2-1"
        _write(usb_dev / "idVendor", "357d")
        if usb_serial is not None:
            _write(usb_dev / "serial", usb_serial)
        scsi = usb_dev / "2-1:1.0/host6/target6:0:0/6:0:0:0"
    elif transport == "virtual":
        scsi = None
    else:
        scsi = sys_root / f"devices/pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:{name}"

    if scsi is None:
        disk_dir = sys_root / "devices/virtual/block" / name
    else:
        _write(scsi / "vendor", vendor)
        _write(scsi / "model", model)
        disk_dir = scsi / "block" / name
    _write(disk_dir / "size", str(sectors))
    _write(disk_dir / "removable", "0")
    _write(disk_dir / "queue/logical_block_size", "512")
    _write(disk_dir / "queue/physical_block_size", "4096")
    (disk_dir / "holders").mkdir(parents=True, exist_ok=True)
    for h in holders or []:
        (disk_dir / "holders" / h).mkdir()
    if scsi is not None:
        (disk_dir / "device").symlink_to(scsi)
    for i, (pname, psectors) in enumerate((partitions or {}).items(), 1):
        _write(disk_dir / pname / "partition", str(i))
        _write(disk_dir / pname / "size", str(psectors))
        (disk_dir / pname / "holders").mkdir(parents=True, exist_ok=True)
        for h in (part_holders or {}).get(pname, []):
            (disk_dir / pname / "holders" / h).mkdir()
        cls = sys_root / "class/block" / pname
        cls.parent.mkdir(parents=True, exist_ok=True)
        cls.symlink_to(disk_dir / pname)
    (sys_root / "block").mkdir(parents=True, exist_ok=True)
    (sys_root / "block" / name).symlink_to(disk_dir)
    return disk_dir


def make_disk(**overrides) -> Disk:
    values = {
        "name": "sdq",
        "path": Path("/dev/sdq"),
        "size": 8_001_563_222_016,
        "vendor": "ST8000DM",
        "model": "004-2CX188",
        "transport": "usb",
        "removable": False,
        "logical_block_size": 512,
        "physical_block_size": 4096,
        "usb_serial": "QP0000FAKE01",
        "holders": [],
        "partitions": [Partition("sdq1", Path("/dev/sdq1"), 8_001_561_124_864)],
    }
    values.update(overrides)
    return Disk(**values)


@pytest.fixture
def sys_root(tmp_path: Path) -> Path:
    root = tmp_path / "sys"
    (root / "block").mkdir(parents=True)
    return root
