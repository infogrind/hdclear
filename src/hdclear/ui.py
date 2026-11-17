"""Everything the user sees: rich-based tables, panels and live progress."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Self

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

from .contents import VolumeInfo
from .disks import Disk
from .safety import SafetyReport
from .smart import SmartChange, SmartInfo
from .surface import BadRange, PhaseResult

console = Console(highlight=False)

STEPS = ("Inspect", "Confirm", "Write", "Verify", "SMART", "Format", "Check")
SPARKS = "▁▂▃▄▅▆▇█"
ACCENT = "cyan"


# -- formatting helpers ----------------------------------------------------


def human(n: int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1000 or unit == "PB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    raise AssertionError("unreachable")


def ago(ts: float | None, now: float | None = None) -> str:
    if ts is None:
        return "?"
    seconds = (now if now is not None else time.time()) - ts
    when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    if seconds < 0:
        return f"{when} (in the future)"
    for unit, size in (("year", 31_557_600), ("month", 2_629_800), ("day", 86_400), ("hour", 3_600)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{when} ({count} {unit}{'s' if count != 1 else ''} ago)"
    return f"{when} (just now)"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def banner() -> None:
    title = Text.assemble(
        ("▞▚ hdclear ", f"bold {ACCENT}"),
        ("wipe · surface-test · ext4", "dim"),
    )
    console.print(title)
    console.print()


def steps_line(current: str | None, done: Sequence[str]) -> Text:
    text = Text()
    for i, step in enumerate(STEPS):
        if i:
            text.append(" ─ ", style="dim")
        if step in done:
            text.append(f"✔ {step}", style="green")
        elif step == current:
            text.append(f"● {step}", style=f"bold {ACCENT}")
        else:
            text.append(f"○ {step}", style="dim")
    return text


# -- disk list ---------------------------------------------------------------


def disk_table(disks: Sequence[Disk], smart_by_name: dict[str, SmartInfo] | None = None) -> Table:
    table = Table(box=box.SIMPLE_HEAVY, header_style=f"bold {ACCENT}")
    table.add_column("Device")
    table.add_column("Size", justify="right")
    table.add_column("Bus")
    table.add_column("Model")
    table.add_column("Serial")
    table.add_column("Partitions")
    for d in disks:
        smart = (smart_by_name or {}).get(d.name)
        serial = (smart.serial if smart else None) or d.usb_serial or ""
        bus_style = "green" if d.transport == "usb" else "dim"
        table.add_row(
            Text(str(d.path), style="bold" if d.transport == "usb" else "dim"),
            human(d.size),
            Text(d.transport, style=bus_style),
            Text((smart.model if smart and smart.model else None) or d.description),
            Text(serial),
            ", ".join(p.name for p in d.partitions) or "-",
        )
    return table


# -- pre-wipe report ---------------------------------------------------------


def identity_panel(disk: Disk, smart: SmartInfo) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column()
    grid.add_row("Device", Text(str(disk.path), style="bold"))
    grid.add_row("Model", smart.model or disk.description)
    grid.add_row("Serial", Text(smart.serial or disk.usb_serial or "unknown", style="bold yellow"))
    grid.add_row("Capacity", f"{human(disk.size)}  ({disk.size:,} bytes)")
    grid.add_row("Connected via", disk.transport.upper())
    if smart.available:
        if smart.rotation_rate:
            grid.add_row("Type", f"HDD, {smart.rotation_rate} rpm")
        elif smart.rotation_rate == 0:
            grid.add_row("Type", "SSD")
        if smart.power_on_hours is not None:
            years = smart.power_on_hours / 8766
            grid.add_row("Powered on", f"{smart.power_on_hours:,} h  (~{years:.1f} years)")
        if smart.temperature is not None:
            grid.add_row("Temperature", f"{smart.temperature} °C")
        health = {True: Text("PASSED", style="green"), False: Text("FAILED", style="bold red")}
        grid.add_row("SMART health", health.get(smart.passed, Text("unknown", style="dim")))
    else:
        grid.add_row("SMART", Text(f"unavailable: {smart.error}", style="yellow"))
    return Panel(grid, title="[bold]Disk[/]", title_align="left", border_style=ACCENT)


def volume_panel(vol: VolumeInfo) -> Panel:
    body: list[RenderableType] = []
    head = Table.grid(padding=(0, 2))
    head.add_column(style="dim", justify="right")
    head.add_column()
    head.add_row("Size", human(vol.size))
    head.add_row("Filesystem", vol.fstype or "none detected")
    if vol.label:
        head.add_row("Label", Text(vol.label, style="bold"))
    if vol.mountpoint and not vol.temp_mounted:
        head.add_row("Mounted at", vol.mountpoint)
    if vol.total:
        used = vol.used or 0
        pct = used / vol.total
        bar = Text("█" * round(pct * 30), style="magenta") + Text("░" * (30 - round(pct * 30)), style="dim")
        head.add_row("Used", Text.assemble(bar, f"  {human(used)} of {human(vol.total)} ({pct:.0%})"))
    if vol.scan:
        s = vol.scan
        at_least = "" if s.complete else "≥ "
        files = f"{at_least}{s.files:,} files in {at_least}{s.dirs:,} folders"
        head.add_row("Files", files + ("" if s.complete else "  [dim](quick scan, stopped early)[/]"))
        if s.newest_mtime is not None:
            head.add_row("Last change", Text(ago(s.newest_mtime), style="bold yellow"))
            head.add_row("", Text(s.newest_path or "", style="dim"))
    body.append(head)

    if vol.top:
        body.append(Text())
        listing = Table.grid(padding=(0, 2))
        listing.add_column()
        listing.add_column(style="dim")
        for e in vol.top:
            name = Text(f"📁 {e.name}/", style="bold blue") if e.is_dir else Text(f"   {e.name}")
            listing.add_row(name, datetime.fromtimestamp(e.mtime).strftime("%Y-%m-%d"))
        if vol.top_more:
            listing.add_row(Text(f"   … and {vol.top_more} more", style="dim"), "")
        body.append(listing)
    elif vol.total is not None:
        body.append(Text("(empty)", style="dim"))

    if vol.note:
        body.append(Text(vol.note, style="yellow"))
    return Panel(Group(*body), title=f"[bold]/dev/{vol.device}[/]", title_align="left", border_style="blue")


def safety_panel(safety: SafetyReport, smart_problems: Sequence[str]) -> Panel | None:
    lines: list[Text] = []
    for b in safety.blockers:
        lines.append(Text(f"⛔ {b}", style="bold red"))
    for w in [*safety.warnings, *smart_problems]:
        lines.append(Text(f"⚠  {w}", style="yellow"))
    if not lines:
        return None
    style = "red" if safety.blockers else "yellow"
    return Panel(Group(*lines), title="[bold]Checks[/]", title_align="left", border_style=style)


def show_report(
    disk: Disk,
    smart: SmartInfo,
    volumes: Sequence[VolumeInfo],
    safety: SafetyReport,
    smart_problems: Sequence[str],
) -> None:
    console.print(identity_panel(disk, smart))
    if not disk.partitions:
        console.print(Text("  No partition table.", style="dim"))
    for vol in volumes:
        console.print(volume_panel(vol))
    panel = safety_panel(safety, smart_problems)
    if panel is not None:
        console.print(panel)


def confirm(disk: Disk, code: str, code_source: str, plan: Sequence[str]) -> bool:
    lines = [Text("This will ", style="bold") + Text("DESTROY ALL DATA", style="bold red reverse")
             + Text(f" on {disk.path} ({human(disk.size)}) and then:", style="bold")]
    lines += [Text(f"  {i}. {step}") for i, step in enumerate(plan, 1)]
    console.print(Panel(Group(*lines), border_style="red", box=box.HEAVY))
    console.print(f"To continue, type the [bold yellow]{code_source}[/] of the disk shown above.")
    try:
        answer = console.input("[bold red]> [/]").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False
    return answer.upper() == code.upper()


# -- live surface-test display ----------------------------------------------


class BytesColumn(ProgressColumn):
    """'20.5 GB/1.0 TB': unlike rich's DownloadColumn, each side picks its own unit."""

    def render(self, task) -> Text:
        return Text(f"{human(int(task.completed))}/{human(int(task.total or 0))}", style="progress.download")


class SurfaceMap:
    """A per-segment throughput sparkline for each phase; ✗ marks errors."""

    def __init__(self, size: int, segments: int = 64):
        self.size = size
        self.segments = segments
        self.data: dict[str, list[list[float]]] = {}  # phase -> [[bytes, seconds, errors], ...]

    def add(self, phase: str, done: int, length: int, seconds: float, error: bool) -> None:
        rows = self.data.setdefault(phase, [[0.0, 0.0, 0.0] for _ in range(self.segments)])
        seg = min(self.segments - 1, (done - 1) * self.segments // self.size)
        rows[seg][0] += length
        rows[seg][1] += seconds
        rows[seg][2] += 1 if error else 0

    def _speed(self, cell: list[float]) -> float | None:
        return cell[0] / cell[1] if cell[1] > 0 else None

    def render(self) -> Table:
        speeds = [s for rows in self.data.values() for c in rows if (s := self._speed(c))]
        top = max(speeds) if speeds else 1.0
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", justify="right")
        grid.add_column()
        for phase in ("write", "verify"):
            line = Text()
            for cell in self.data.get(phase, [[0, 0, 0]] * self.segments):
                speed = self._speed(cell)
                if cell[2]:
                    line.append("✗", style="bold red")
                elif speed is None:
                    line.append("·", style="dim")
                else:
                    rel = speed / top
                    style = "green" if rel >= 0.4 else "yellow"
                    line.append(SPARKS[min(len(SPARKS) - 1, int(rel * len(SPARKS)))], style=style)
            grid.add_row(phase, line)
        grid.add_row("", Text("start of disk" + " " * (self.segments - 24) + "end of disk", style="dim"))
        return grid


class SurfaceMonitor:
    """Live view for the write + verify passes; also logs progress to the log file."""

    def __init__(
        self, disk: Disk, model: str | None, serial: str | None, log: RunLog, done_steps: list[str]
    ):
        self.disk = disk
        self.model = model or disk.description
        self.serial = serial
        self.log = log
        self.done_steps = done_steps
        self.current = "Write"
        self.bad_count: Callable[[], int] = lambda: 0
        self.map = SurfaceMap(disk.size, segments=min(64, max(24, console.width - 20)))
        self.progress = Progress(
            TextColumn("[bold]{task.description:<7}"),
            BarColumn(bar_width=None, complete_style=ACCENT, finished_style="green"),
            TaskProgressColumn(),
            BytesColumn(),
            TransferSpeedColumn(),
            TextColumn("[dim]elapsed[/]"),
            TimeElapsedColumn(),
            TextColumn("[dim]left[/]"),
            TimeRemainingColumn(),
            expand=True,
            console=console,
        )
        self.tasks = {
            "write": self.progress.add_task("Write", total=disk.size, start=False),
            "verify": self.progress.add_task("Verify", total=disk.size, start=False),
        }
        self.overall = self.progress.add_task("Total", total=2 * disk.size)
        self.live = Live(self, console=console, refresh_per_second=2, transient=False)
        self._last_logged_pct: dict[str, int] = {}

    def __rich__(self) -> Panel:
        bad_sectors = self.bad_count()
        bad = (
            Text(f"{bad_sectors} bad sector(s)", style="bold red")
            if bad_sectors
            else Text("no errors so far", style="green")
        )
        title = f"[bold]{self.disk.path}[/] · {escape(self.model)}"
        if self.serial:
            title += f" · SN {escape(self.serial)}"
        return Panel(
            Group(
                steps_line(self.current, self.done_steps),
                Text(),
                self.progress,
                Text(),
                self.map.render(),
                Text(),
                Text.assemble(("Status: ", "dim"), bad, ("   ·   detach freely, e.g. tmux Ctrl-b d", "dim")),
            ),
            title=title,
            title_align="left",
            border_style=ACCENT,
            box=box.ROUNDED,
        )

    def __enter__(self) -> Self:
        self.live.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.live.__exit__(*exc)

    def start_phase(self, phase: str) -> None:
        self.current = phase.capitalize()
        self.progress.start_task(self.tasks[phase])
        self.log.write(f"{phase} pass started")

    def finish_phase(self, phase: str, result: PhaseResult) -> None:
        self.done_steps.append(phase.capitalize())
        self.log.write(
            f"{phase} pass finished: {human(result.bytes_done)} in {duration(result.seconds)}, "
            f"{result.mb_per_s:.0f} MB/s, {len(result.bad)} bad range(s)"
        )

    def __call__(self, phase: str, done: int, length: int, seconds: float, error: bool) -> None:
        self.progress.update(self.tasks[phase], completed=done)
        offset = 0 if phase == "write" else self.disk.size
        self.progress.update(self.overall, completed=offset + done)
        self.map.add(phase, done, length, seconds, error)
        if error:
            self.log.write(f"{phase}: error in chunk ending at byte {done}")
        pct = done * 100 // self.disk.size
        if pct != self._last_logged_pct.get(phase):
            self._last_logged_pct[phase] = pct
            self.log.write(f"{phase}: {pct}% ({human(done)})")


# -- results -------------------------------------------------------------------


def bad_ranges_table(ranges: Sequence[BadRange], limit: int = 15) -> Table:
    table = Table(box=box.SIMPLE, header_style="bold red")
    table.add_column("Phase")
    table.add_column("Byte offset", justify="right")
    table.add_column("Length", justify="right")
    table.add_column("Problem")
    for r in ranges[:limit]:
        table.add_row(r.phase, f"{r.offset:,}", human(r.length), r.detail)
    if len(ranges) > limit:
        table.add_row("…", f"{len(ranges) - limit} more in the log", "", "")
    return table


def smart_changes_table(changes: Sequence[SmartChange]) -> Table:
    table = Table(box=box.SIMPLE, header_style="bold yellow")
    table.add_column("SMART attribute")
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    for c in changes:
        style = "red" if c.critical else "yellow"
        table.add_row(Text(f"{c.attr_id} {c.name}", style=style), str(c.before), str(c.after))
    return table


def verdict(passed: bool, lines: Sequence[str], extra: Sequence[RenderableType] = ()) -> None:
    if passed:
        head = Text("  ✔  PASSED  ", style="bold black on green")
        border = "green"
    else:
        head = Text("  ✘  FAILED  ", style="bold white on red")
        border = "red"
    body: list[RenderableType] = [head, Text()]
    body += [Text(line) for line in lines]
    body += list(extra)
    console.print(Panel(Group(*body), border_style=border, box=box.HEAVY))


# -- log file -----------------------------------------------------------------


class RunLog:
    def __init__(self, path: Path | None):
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str) -> None:
        if self.path is None:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp}  {message}\n")
