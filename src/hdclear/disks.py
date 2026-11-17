"""Disk discovery straight from sysfs and /proc.

Synology DSM is a stripped-down Linux where tools like ``lsblk`` may be
missing or old, so everything here reads the kernel's own view of the world.
All roots are parameters so tests can point them at a fake tree.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .errors import HdclearError

SYS = Path("/sys")
PROC = Path("/proc")
DEV = Path("/dev")

# Block devices that are never physical disks.
_SKIP_PREFIXES = ("loop", "ram", "zram", "dm-", "md", "sr", "fd", "nbd", "synoboot")


@dataclass
class Partition:
    name: str
    path: Path
    size: int
    holders: list[str] = field(default_factory=list)


@dataclass
class Disk:
    name: str
    path: Path
    size: int
    vendor: str
    model: str
    transport: str  # usb | sata | nvme | virtual | other
    removable: bool
    logical_block_size: int
    physical_block_size: int
    usb_serial: str | None
    holders: list[str]
    partitions: list[Partition]

    @property
    def all_names(self) -> set[str]:
        return {self.name} | {p.name for p in self.partitions}

    @property
    def description(self) -> str:
        return " ".join(x for x in (self.vendor, self.model) if x) or "unknown"


@dataclass
class Mount:
    device: str  # kernel name, e.g. "sdq1"
    mountpoint: str
    fstype: str


@dataclass
class SystemState:
    mounts: list[Mount]
    swaps: set[str]
    md_members: set[str]


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(_read(path))
    except ValueError:
        return default


def _holders(block_dir: Path) -> list[str]:
    try:
        return sorted(p.name for p in (block_dir / "holders").iterdir())
    except OSError:
        return []


def _transport(name: str, block_dir: Path) -> str:
    real = str(block_dir.resolve())
    if "/usb" in real:
        return "usb"
    if "/virtual/" in real:
        return "virtual"
    if name.startswith("nvme"):
        return "nvme"
    if "/ata" in real or name.startswith("sata"):
        return "sata"
    return "other"


def _usb_serial(block_dir: Path) -> str | None:
    """Walk up the device chain to the USB device node and read its serial."""
    node = (block_dir / "device").resolve()
    for _ in range(12):
        serial = node / "serial"
        if (node / "idVendor").exists() and serial.exists():
            return _read(serial) or None
        if node.parent == node:
            break
        node = node.parent
    return None


def read_disk(name: str, sys_root: Path = SYS, dev_root: Path = DEV) -> Disk:
    block_dir = sys_root / "block" / name
    if not block_dir.is_dir():
        raise HdclearError(f"{name} is not a whole block device")
    partitions = []
    for child in sorted(block_dir.iterdir()):
        if (child / "partition").exists():
            partitions.append(
                Partition(
                    name=child.name,
                    path=dev_root / child.name,
                    size=_read_int(child / "size") * 512,
                    holders=_holders(child),
                )
            )
    partitions.sort(key=lambda p: _read_int(block_dir / p.name / "partition"))
    return Disk(
        name=name,
        path=dev_root / name,
        size=_read_int(block_dir / "size") * 512,
        vendor=_read(block_dir / "device" / "vendor"),
        model=_read(block_dir / "device" / "model"),
        transport=_transport(name, block_dir),
        removable=_read(block_dir / "removable") == "1",
        logical_block_size=_read_int(block_dir / "queue" / "logical_block_size", 512) or 512,
        physical_block_size=_read_int(block_dir / "queue" / "physical_block_size", 512) or 512,
        usb_serial=_usb_serial(block_dir),
        holders=_holders(block_dir),
        partitions=partitions,
    )


def list_disks(sys_root: Path = SYS, dev_root: Path = DEV) -> list[Disk]:
    block = sys_root / "block"
    try:
        names = sorted(p.name for p in block.iterdir())
    except OSError:
        return []
    disks = []
    for name in names:
        if name.startswith(_SKIP_PREFIXES):
            continue
        disk = read_disk(name, sys_root, dev_root)
        if disk.size > 0:
            disks.append(disk)
    return disks


def resolve_device(arg: str, sys_root: Path = SYS) -> str:
    """Turn '/dev/sdq', 'sdq' or a /dev/disk/by-id symlink into a kernel disk name."""
    name = Path(arg).resolve().name if "/" in arg else arg
    if (sys_root / "block" / name).is_dir():
        return name
    if (sys_root / "class" / "block" / name / "partition").exists():
        raise HdclearError(f"{arg} is a partition; give the whole disk (e.g. /dev/sdq, not /dev/sdq1)")
    raise HdclearError(f"no such disk: {arg}")


def _mount_device_name(source: str) -> str | None:
    if not source.startswith("/dev/"):
        return None
    try:
        return Path(source).resolve().name
    except OSError:
        return Path(source).name


def _unescape_mount(path: str) -> str:
    # /proc/mounts escapes space, tab, newline and backslash as octal.
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        path = path.replace(code, char)
    return path


def read_system_state(proc_root: Path = PROC) -> SystemState:
    mounts = []
    for line in _read(proc_root / "mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev = _mount_device_name(parts[0])
        if dev:
            mounts.append(Mount(dev, _unescape_mount(parts[1]), parts[2]))

    swaps = set()
    for line in _read(proc_root / "swaps").splitlines()[1:]:
        if line.split():
            dev = _mount_device_name(line.split()[0])
            if dev:
                swaps.add(dev)

    md_members = set()
    for line in _read(proc_root / "mdstat").splitlines():
        if " : " in line and line.startswith("md"):
            for token in line.split(" : ", 1)[1].split():
                if "[" in token:
                    md_members.add(token.split("[", 1)[0])
    return SystemState(mounts, swaps, md_members)


def mounts_of(disk: Disk, state: SystemState) -> list[Mount]:
    return [m for m in state.mounts if m.device in disk.all_names]


def partition_device_name(disk_name: str, number: int) -> str:
    sep = "p" if disk_name[-1].isdigit() else ""
    return f"{disk_name}{sep}{number}"


def is_block_device(path: Path) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False
