"""Peek at what is currently on a disk so the user recognises it before wiping.

Unmounted filesystems are mounted read-only (without journal replay) into a
temporary directory, looked at, and unmounted again.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import tools
from .disks import Disk, Mount

MOUNTABLE = {"ext2", "ext3", "ext4", "vfat", "exfat", "ntfs", "ntfs3", "btrfs", "xfs", "hfsplus"}
RO_OPTIONS = {
    "ext3": "ro,noload",
    "ext4": "ro,noload",
    "xfs": "ro,norecovery",
    "btrfs": "ro,nologreplay",
}

SCAN_MAX_ENTRIES = 50_000
SCAN_MAX_SECONDS = 8.0
TOP_ENTRIES = 12


@dataclass
class Entry:
    name: str
    is_dir: bool
    mtime: float


@dataclass
class ScanResult:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    newest_mtime: float | None = None
    newest_path: str | None = None
    complete: bool = True


@dataclass
class VolumeInfo:
    device: str
    size: int
    fstype: str | None = None
    label: str | None = None
    mountpoint: str | None = None  # where it was (or got temporarily) mounted
    temp_mounted: bool = False
    total: int | None = None
    used: int | None = None
    top: list[Entry] = field(default_factory=list)
    top_more: int = 0
    scan: ScanResult | None = None
    note: str | None = None


def probe_fs(device: Path) -> dict[str, str]:
    """Return blkid's TYPE/LABEL/UUID etc. for a device (empty if unknown)."""
    blkid = tools.find_tool("blkid")
    if blkid is None:
        return {}
    proc = tools.run([blkid, "-p", "-o", "export", str(device)], check=False)
    result = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def list_top(root: Path) -> tuple[list[Entry], int]:
    entries = []
    with os.scandir(root) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
                entries.append(Entry(e.name, e.is_dir(follow_symlinks=False), st.st_mtime))
            except OSError:
                continue
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries[:TOP_ENTRIES], max(0, len(entries) - TOP_ENTRIES)


def scan_tree(
    root: Path, max_entries: int = SCAN_MAX_ENTRIES, max_seconds: float = SCAN_MAX_SECONDS
) -> ScanResult:
    """Bounded walk: count files, sum sizes, find the most recently modified file."""
    result = ScanResult()
    deadline = time.monotonic() + max_seconds
    stack = [root]
    seen = 0
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it:
            for e in it:
                seen += 1
                if seen > max_entries or time.monotonic() > deadline:
                    result.complete = False
                    return result
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if e.is_dir(follow_symlinks=False):
                    result.dirs += 1
                    stack.append(Path(e.path))
                    continue
                result.files += 1
                result.bytes += st.st_size
                if result.newest_mtime is None or st.st_mtime > result.newest_mtime:
                    result.newest_mtime = st.st_mtime
                    result.newest_path = os.path.relpath(e.path, root)
    return result


def inspect_mounted(info: VolumeInfo, root: Path) -> None:
    st = os.statvfs(root)
    info.total = st.f_blocks * st.f_frsize
    info.used = (st.f_blocks - st.f_bfree) * st.f_frsize
    info.top, info.top_more = list_top(root)
    info.scan = scan_tree(root)


def _temp_mount(device: Path, fstype: str) -> Path:
    mount = tools.require_tool("mount")
    target = Path(tempfile.mkdtemp(prefix="hdclear-peek-"))
    options = RO_OPTIONS.get(fstype, "ro")
    proc = tools.run([mount, "-t", fstype, "-o", options, str(device), str(target)], check=False)
    if proc.returncode != 0:
        proc = tools.run([mount, "-o", "ro", str(device), str(target)], check=False)
    if proc.returncode != 0:
        target.rmdir()
        raise OSError((proc.stderr or proc.stdout).strip() or "mount failed")
    return target


def _temp_unmount(target: Path) -> None:
    umount = tools.find_tool("umount") or "umount"
    tools.run([umount, str(target)], check=False)
    try:
        target.rmdir()
    except OSError:
        pass


def inspect_volume(device: Path, size: int, mounts: list[Mount]) -> VolumeInfo:
    info = VolumeInfo(device=device.name, size=size)
    probe = probe_fs(device)
    info.fstype = probe.get("TYPE")
    info.label = probe.get("LABEL")
    if probe.get("PTTYPE") and not info.fstype:
        info.note = f"{probe['PTTYPE']} partition table"

    existing = next((m for m in mounts if m.device == device.name), None)
    if existing is not None:
        info.fstype = info.fstype or existing.fstype
        info.mountpoint = existing.mountpoint
        try:
            inspect_mounted(info, Path(existing.mountpoint))
        except OSError as e:
            info.note = f"could not read contents: {e}"
        return info

    if info.fstype == "linux_raid_member":
        info.note = "Synology/mdadm RAID member - contents not readable directly"
        return info
    if info.fstype not in MOUNTABLE:
        if info.fstype and not info.note:
            info.note = f"{info.fstype}: not mountable for inspection"
        return info

    try:
        target = _temp_mount(device, info.fstype)
    except OSError as e:
        info.note = f"could not mount read-only for inspection: {e}"
        return info
    info.mountpoint = str(target)
    info.temp_mounted = True
    try:
        inspect_mounted(info, target)
    except OSError as e:
        info.note = f"could not read contents: {e}"
    finally:
        _temp_unmount(target)
    return info


def inspect_disk(disk: Disk, mounts: list[Mount]) -> list[VolumeInfo]:
    if disk.partitions:
        return [inspect_volume(p.path, p.size, mounts) for p in disk.partitions]
    return [inspect_volume(disk.path, disk.size, mounts)]
