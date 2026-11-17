"""Hard safety checks: reasons to refuse touching a disk at all.

Blockers stop hdclear outright (no override except where noted). Warnings are
shown prominently in the pre-wipe report but don't stop anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .disks import Disk, SystemState, mounts_of

# Mount points under these prefixes belong to removable media; hdclear will
# unmount them itself. Anything mounted elsewhere is part of the running system.
REMOVABLE_MOUNT_PREFIXES = ("/volumeUSB", "/volumeSATA", "/media/", "/mnt/", "/run/media/")

# Mount points that are always part of the running system, even for a disk that
# is also mounted somewhere removable.
_SYSTEM_MOUNT = re.compile(r"^/(?:|boot|usr|var|etc|opt|home|root|volume\d+)$")


def is_removable_mount(mountpoint: str) -> bool:
    return mountpoint.startswith(REMOVABLE_MOUNT_PREFIXES)


@dataclass
class SafetyReport:
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


def check_disk(disk: Disk, state: SystemState, allow_internal: bool = False) -> SafetyReport:
    report = SafetyReport()

    if disk.size == 0:
        report.blockers.append("disk reports a size of 0 bytes (no medium inserted?)")

    if disk.transport != "usb":
        msg = f"disk is connected via {disk.transport}, not USB"
        if allow_internal:
            report.warnings.append(msg + " (allowed by --allow-internal)")
        else:
            report.blockers.append(msg + "; pass --allow-internal if you really mean it")

    if disk.holders:
        report.blockers.append(f"disk is in use by {', '.join(disk.holders)} (RAID/LVM/device-mapper)")
    for part in disk.partitions:
        if part.holders:
            report.blockers.append(
                f"{part.name} is in use by {', '.join(part.holders)} (RAID/LVM/device-mapper)"
            )

    for name in sorted(disk.all_names & state.md_members):
        report.blockers.append(f"{name} is a member of a software RAID (/proc/mdstat)")

    for name in sorted(disk.all_names & state.swaps):
        report.blockers.append(f"{name} is active swap space")

    mounts = mounts_of(disk, state)
    removable_devices = {m.device for m in mounts if is_removable_mount(m.mountpoint)}
    for mount in mounts:
        if is_removable_mount(mount.mountpoint):
            report.warnings.append(
                f"{mount.device} is mounted at {mount.mountpoint}; it will be unmounted"
            )
        elif mount.device in removable_devices and not _SYSTEM_MOUNT.match(mount.mountpoint):
            # A second view of a removable mount, e.g. DSM's Container Manager
            # bind-mounting USB shares under /volume1/@appdata/ContainerManager/.
            report.warnings.append(
                f"{mount.device} is also mounted at {mount.mountpoint} "
                "(a second mount of the same USB partition); it will be unmounted"
            )
        else:
            report.blockers.append(
                f"{mount.device} is mounted at {mount.mountpoint}, which looks like a system path"
            )

    return report
