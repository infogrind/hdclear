# hdclear

Wipe, surface-test and ext4-format an external disk before using it as a
backup disk. Built for a Synology DiskStation with a USB dock (e.g. Sharkoon
QuickPort), works on any Linux.

## What it does

1. **Inspect** – shows the disk's model, serial, capacity, power-on hours and
   SMART health, and for every partition: filesystem, label, used space, the
   top-level folders, file count and *when something was last written*.
   Unmounted filesystems are mounted read-only (no journal replay) to look
   inside. This is so you notice when you plugged in the wrong disk.
1. **Refuse** anything that looks like a system disk: not on USB, part of a
   RAID/LVM, swap, or mounted outside the removable-media paths
   (`/volumeUSB*`, `/media`, `/mnt`, …).
1. **Confirm** – you type the last 4 characters of the disk's serial number
   (not just "yes").
1. **Write + verify** – writes a pattern over every byte, then reads it all
   back and compares. Each 8 MiB chunk carries its own offset, so "fake
   capacity" disks that wrap around are caught too. Uses `O_DIRECT`, so the
   verify pass really reads the disk and not the page cache. I/O errors are
   narrowed down to the sector.
1. **SMART** – compares reallocated/pending/uncorrectable counters before and
   after.
1. **Format** – GPT with one partition, `mkfs.ext4 -m 0`, then `e2fsck -f -n`.
   A disk that failed is wiped but *not* formatted.

A live view shows progress, speed, ETA and a speed map of the disk surface
(slow or failing areas stand out). A log goes to
`$XDG_STATE_HOME/hdclear/` (for root usually `/root/.local/state/hdclear/`).

## Usage

```shell
hdclear                         # list disks
sudo hdclear -n /dev/sdq        # dry run: inspect only, change nothing
sudo hdclear /dev/sdq           # the real thing (run inside tmux)
sudo hdclear -L backup1 /dev/sdq
```

`sudo` drops the Homebrew prefix from `PATH`, so use the full path, e.g.
`sudo "$(which hdclear)" /dev/sdq`. hdclear itself finds its helper tools
in PATH, the sbin directories and its own Homebrew prefix.

Exit codes: `0` passed, `1` error / aborted before wiping, `2` disk failed
the test, `130` interrupted.

A full run reads and writes the disk once each: expect roughly 10–20 hours for
8 TB over USB 3.

## Requirements

Linux, root, and these tools (DSM ships most of them; Homebrew can provide the
rest): `mkfs.ext4`, `e2fsck` (e2fsprogs), one of `sgdisk` / `sfdisk` /
`parted`, `umount`/`mount`, optionally `smartctl` (smartmontools, strongly
recommended) and `blkid`.

## Trying it safely on a loop device

```shell
truncate -s 2G /tmp/hdclear-test.img
sudo losetup -fP --show /tmp/hdclear-test.img      # prints e.g. /dev/loop3
sudo "$(which hdclear)" --allow-internal /dev/loop3  # confirm by typing "loop3"
sudo losetup -d /dev/loop3
```

## Development

```shell
uv run pytest
uv run ruff check .
```
