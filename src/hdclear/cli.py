"""Command-line entry point.

``main(argv)`` parses arguments and is the single error boundary;
``_run(...)`` does the work with plain arguments and returns an exit code.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.text import Text

from . import __version__, contents, disks, formatter, safety, smart, ui
from .errors import HdclearError
from .surface import DiskGoneError, PhaseResult, SurfaceTest, TooManyErrorsError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DISK_FAILED = 2
EXIT_INTERRUPTED = 130

ASSUMED_MB_PER_S = 150  # for the up-front duration estimate only


@dataclass
class Options:
    device: str | None
    dry_run: bool
    label: str | None
    allow_internal: bool
    max_bad_sectors: int
    log_dir: Path | None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hdclear",
        description=(
            "Wipe an external disk, test every sector with a full write + read-back pass, "
            "check SMART, and format it as ext4 for use as a backup disk. "
            "Without DEVICE, lists the disks hdclear can see."
        ),
    )
    parser.add_argument("device", nargs="?", help="whole disk to wipe, e.g. /dev/sdq")
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="inspect and show the report, change nothing"
    )
    parser.add_argument("-L", "--label", help="filesystem label for the new ext4 partition")
    parser.add_argument(
        "--allow-internal",
        action="store_true",
        help="allow disks that are not connected via USB (dangerous on a NAS)",
    )
    parser.add_argument(
        "--max-bad-sectors",
        type=int,
        default=256,
        metavar="N",
        help="abort the surface test after N bad sectors (default: %(default)s)",
    )
    parser.add_argument(
        "--log-dir", type=Path, help="where to write the log (default: $XDG_STATE_HOME/hdclear)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def default_log_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME", "")
    base = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local" / "state"
    return base / "hdclear"


def check_environment() -> None:
    if not sys.platform.startswith("linux"):
        raise HdclearError("hdclear only works on Linux")
    if os.geteuid() != 0:
        raise HdclearError("hdclear needs root to access disks; run it with sudo")


def confirmation_code(serial: str | None, disk_name: str) -> tuple[str, str]:
    """Return (code the user must type, human description of where to find it)."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", serial or "")
    if len(cleaned) >= 4:
        return cleaned[-4:], "last 4 characters of the serial number"
    return disk_name, f"device name ({disk_name})"


def plan_steps(disk: disks.Disk, label: str | None) -> list[str]:
    hours = 2 * disk.size / (ASSUMED_MB_PER_S * 1e6) / 3600
    return [
        (
            f"write a test pattern over all {ui.human(disk.size)} and read every byte back "
            f"(roughly {hours:.0f}-{hours * 1.5:.0f} h over USB)"
        ),
        "compare SMART error counters before and after",
        "create a GPT partition table with one ext4 partition"
        + (f" labelled '{label}'" if label else ""),
        "verify the new filesystem with e2fsck",
    ]


def _list_disks() -> int:
    found = disks.list_disks()
    if not found:
        ui.console.print("No disks found.")
        return EXIT_OK
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    smart_by_name = {d.name: smart.query(d.path) for d in found} if is_root else {}
    ui.console.print(ui.disk_table(found, smart_by_name))
    if not is_root:
        ui.console.print("[dim]Run as root to also see the disks' own serial numbers.[/]")
    ui.console.print("Run [bold]hdclear /dev/sdX[/] to inspect one ([bold]-n[/] for a dry run).")
    return EXIT_OK


def _run(opts: Options) -> int:
    ui.banner()
    if opts.device is None:
        return _list_disks()

    check_environment()
    disk = disks.read_disk(disks.resolve_device(opts.device))
    state = disks.read_system_state()

    with ui.console.status("Reading SMART data and peeking at the disk's contents…"):
        smart_before = smart.query(disk.path)
        report = safety.check_disk(disk, state, opts.allow_internal)
        volumes = contents.inspect_disk(disk, disks.mounts_of(disk, state))

    missing = [name for name, path in formatter.required_tools().items() if path is None]
    if missing:
        report.blockers.append(f"missing tools: {', '.join(missing)}")
    if smart_before.available and smart_before.capacity and smart_before.capacity != disk.size:
        report.warnings.append(
            f"SMART reports {ui.human(smart_before.capacity)} but the dock exposes "
            f"{ui.human(disk.size)} - the USB dock may be limiting capacity"
        )

    ui.show_report(disk, smart_before, volumes, report, smart.existing_problems(smart_before))

    if not report.ok:
        raise HdclearError("refusing to touch this disk (see Checks above)")
    if opts.dry_run:
        ui.console.print("[dim]Dry run - nothing was changed.[/]")
        return EXIT_OK

    serial = smart_before.serial or disk.usb_serial
    code, code_source = confirmation_code(serial, disk.name)
    if not sys.stdin.isatty():
        raise HdclearError("refusing to wipe without an interactive terminal to confirm on")
    if not ui.confirm(disk, code, code_source, plan_steps(disk, opts.label)):
        ui.console.print("[yellow]Confirmation did not match - nothing was changed.[/]")
        return EXIT_ERROR

    log_path = (opts.log_dir or default_log_dir()) / (
        f"{datetime.now():%Y%m%d-%H%M%S}-{disk.name}-{re.sub(r'[^A-Za-z0-9]', '', serial or 'noserial')}.log"
    )
    log = ui.RunLog(log_path)
    log.write(f"hdclear {__version__}: {disk.path} {disk.description} serial={serial} size={disk.size}")
    ui.console.print(f"[dim]Log: {log_path}[/]")

    try:
        return _wipe_and_test(disk, state, smart_before, serial, opts, log)
    except KeyboardInterrupt:
        log.write("interrupted by user")
        ui.console.print(
            "\n[bold yellow]Interrupted.[/] The disk is partly overwritten and has no usable "
            "filesystem. Run hdclear again to start over."
        )
        return EXIT_INTERRUPTED


def _wipe_and_test(
    disk: disks.Disk,
    state: disks.SystemState,
    smart_before: smart.SmartInfo,
    serial: str | None,
    opts: Options,
    log: ui.RunLog,
) -> int:
    done = ["Inspect", "Confirm"]

    mounted = disks.mounts_of(disk, state)
    if mounted:
        formatter.unmount(mounted)
        log.write(f"unmounted {', '.join(m.mountpoint for m in mounted)}")

    monitor = ui.SurfaceMonitor(disk, smart_before.model, serial, log, done)
    test = SurfaceTest(
        disk.path,
        disk.size,
        sector=max(disk.physical_block_size, disk.logical_block_size),
        max_bad_sectors=opts.max_bad_sectors,
        progress=monitor,
    )
    monitor.bad_count = lambda: test.bad_sectors
    results: list[PhaseResult] = []
    aborted: str | None = None
    with monitor:
        try:
            for phase, run_pass in (("write", test.write_pass), ("verify", test.read_pass)):
                monitor.start_phase(phase)
                result = run_pass()
                results.append(result)
                monitor.finish_phase(phase, result)
        except TooManyErrorsError as e:
            aborted = str(e)
        except DiskGoneError as e:
            log.write(str(e))
            raise
    log.write(f"I/O mode: {'O_DIRECT' if test.direct else 'buffered'}")
    if aborted:
        log.write(aborted)

    bad_ranges = [r for res in results for r in res.bad]
    for r in bad_ranges:
        log.write(f"BAD {r.phase} offset={r.offset} length={r.length} {r.detail}")

    with ui.console.status("Re-reading SMART data…"):
        smart_after = smart.query(disk.path)
    changes = smart.compare(smart_before, smart_after) if smart_after.available else []
    for c in changes:
        log.write(f"SMART {c.attr_id} {c.name}: {c.before} -> {c.after}")
    critical = [c for c in changes if c.critical]
    if smart_after.passed is False:
        log.write("SMART overall health: FAILED")
    done.append("SMART")

    stats = [
        f"{res.phase.capitalize():<7} {ui.human(res.bytes_done)} in {ui.duration(res.seconds)} "
        f"(avg {res.mb_per_s:.0f} MB/s)"
        for res in results
    ]
    failed = bool(aborted or bad_ranges or critical or smart_after.passed is False)
    if failed:
        extra: list = []
        if bad_ranges:
            extra.append(ui.bad_ranges_table(bad_ranges))
        if changes:
            extra.append(ui.smart_changes_table(changes))
        reasons = []
        if aborted:
            reasons.append(aborted)
        if bad_ranges:
            reasons.append(f"{test.bad_sectors} bad sector(s) in {len(bad_ranges)} range(s)")
        if critical:
            reasons.append("SMART error counters went up during the test")
        if smart_after.passed is False:
            reasons.append("SMART overall health self-assessment FAILED")
        log.write("RESULT: FAILED - " + "; ".join(reasons))
        ui.verdict(
            False,
            [*reasons, *stats, "", "The disk was wiped but NOT formatted. Don't use it for backups."],
            extra,
        )
        return EXIT_DISK_FAILED

    with ui.console.status("Creating GPT partition table and ext4 filesystem…"):
        formatter.partition(disk)
        part = formatter.reread_partitions(disk)
        formatter.make_ext4(part, opts.label)
    done.append("Format")
    log.write(f"created ext4 on {part}")
    with ui.console.status("Checking the new filesystem…"):
        fsck_summary = formatter.check_ext4(part)
    done.append("Check")
    log.write(f"e2fsck: {fsck_summary}")
    log.write("RESULT: PASSED")

    ui.console.print(ui.steps_line(None, done))
    lines = [
        f"Every byte of {disk.path} ({ui.human(disk.size)}) was written and read back without errors.",
        *stats,
        "SMART: no disk error counters increased"
        if smart_after.available
        else f"SMART: not available ({smart_after.error})",
        f"New filesystem: ext4 on {part}" + (f", label '{opts.label}'" if opts.label else ""),
        f"e2fsck: {fsck_summary}",
        "",
        "Re-plug the disk (or eject/reinsert it in DSM) so it gets mounted.",
    ]
    extra = []
    if changes:  # only non-critical (link) counters can have changed on a pass
        extra.append(ui.smart_changes_table(changes))
        lines.insert(-2, "Note: UDMA CRC errors went up - usually a cable/dock issue, not the disk.")
    ui.verdict(True, lines, extra)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    opts = Options(
        device=args.device,
        dry_run=args.dry_run,
        label=args.label,
        allow_internal=args.allow_internal,
        max_bad_sectors=args.max_bad_sectors,
        log_dir=args.log_dir,
    )
    try:
        return _run(opts)
    except HdclearError as e:
        ui.console.print(Text("Error: ", style="bold red") + Text(str(e)))
        return EXIT_ERROR
    except KeyboardInterrupt:
        ui.console.print("\nAborted - nothing was changed.")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
