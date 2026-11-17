"""Unmounting, partitioning (GPT, one partition) and creating/checking ext4."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from . import tools
from .disks import Disk, Mount, partition_device_name
from .errors import HdclearError

PARTITIONERS = ("sgdisk", "sfdisk", "parted")


def required_tools() -> dict[str, str | None]:
    """Map of tool -> path (None if missing). Partitioning needs any one of PARTITIONERS."""
    found = {name: tools.find_tool(name) for name in ("mkfs.ext4", "e2fsck", "umount")}
    found["partitioner"] = next(
        (p for p in (tools.find_tool(n) for n in PARTITIONERS) if p is not None), None
    )
    return found


def unmount(mounts: list[Mount]) -> None:
    umount = tools.require_tool("umount")
    # Deepest mount points first.
    for m in sorted(mounts, key=lambda m: m.mountpoint.count("/"), reverse=True):
        proc = tools.run([umount, m.mountpoint], check=False)
        if proc.returncode != 0:
            raise HdclearError(
                f"could not unmount {m.mountpoint}: {(proc.stderr or proc.stdout).strip()}\n"
                "Close whatever uses it (or eject it in DSM's External Devices panel) and retry."
            )


def partition(disk: Disk) -> None:
    dev = str(disk.path)
    tool = required_tools()["partitioner"]
    if tool is None:
        raise HdclearError(f"no partitioning tool found (need one of: {', '.join(PARTITIONERS)})")
    name = Path(tool).name
    if name == "sgdisk":
        tools.run([tool, "--zap-all", dev], check=False)  # exits 2 on a blank disk; harmless
        tools.run([tool, "--new=1:0:0", "--typecode=1:8300", "--change-name=1:backup", dev])
    elif name == "sfdisk":
        tools.run([tool, "--wipe", "always", dev], input="label: gpt\n,,L\n")
    else:
        tools.run([tool, "-s", dev, "mklabel", "gpt", "mkpart", "backup", "ext4", "1MiB", "100%"])


def reread_partitions(disk: Disk, wait: Callable[[float], None] = time.sleep) -> Path:
    """Get the kernel to notice the new partition and return its device path."""
    for name, args in (("blockdev", ["--rereadpt"]), ("partprobe", []), ("partx", ["-u"])):
        tool = tools.find_tool(name)
        if tool:
            tools.run([tool, *args, str(disk.path)], check=False)
    part = disk.path.parent / partition_device_name(disk.name, 1)
    for _ in range(40):
        if part.exists():
            return part
        wait(0.25)
    raise HdclearError(
        f"{part} did not appear after partitioning. Unplug/replug the disk and run "
        f"'mkfs.ext4 {part}' yourself."
    )


def make_ext4(part: Path, label: str | None) -> None:
    args = [
        tools.require_tool("mkfs.ext4"),
        "-F",
        "-m",
        "0",  # no root-reserved blocks on a pure data disk
        "-E",
        "lazy_itable_init=0,lazy_journal_init=0",  # initialise now, not in the background later
    ]
    if label:
        args += ["-L", label]
    tools.run([*args, str(part)])


def check_ext4(part: Path) -> str:
    proc = tools.run([tools.require_tool("e2fsck"), "-f", "-n", str(part)], check=False)
    if proc.returncode != 0:
        raise HdclearError(f"e2fsck reported problems on the fresh filesystem:\n{proc.stdout}{proc.stderr}")
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "clean"
