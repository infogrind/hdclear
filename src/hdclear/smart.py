"""SMART data via smartctl's JSON output.

USB docks (e.g. Sharkoon QuickPort) need SCSI-ATA translation passthrough;
smartctl usually auto-detects it, otherwise we retry with ``-d sat`` and
``-d sat,12``. SMART being unavailable is a warning, never fatal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import tools

DEVICE_TYPES = (None, "sat", "sat,12")

# Attributes whose raw value should never go up during a test.
CRITICAL = {
    5: "Reallocated sectors",
    10: "Spin retries",
    187: "Reported uncorrectable",
    196: "Reallocation events",
    197: "Pending sectors",
    198: "Offline uncorrectable",
}
# Attributes that hint at cable/dock trouble rather than the disk itself.
LINK = {199: "UDMA CRC errors"}


@dataclass
class SmartInfo:
    available: bool
    device_type: str | None = None
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    capacity: int | None = None
    rotation_rate: int | None = None
    power_on_hours: int | None = None
    temperature: int | None = None
    passed: bool | None = None
    attributes: dict[int, tuple[str, int]] = field(default_factory=dict)
    error: str | None = None

    def raw(self, attr_id: int) -> int | None:
        entry = self.attributes.get(attr_id)
        return entry[1] if entry else None


@dataclass
class SmartChange:
    attr_id: int
    name: str
    before: int
    after: int
    critical: bool


def parse(data: dict, device_type: str | None) -> SmartInfo:
    info = SmartInfo(available=True, device_type=device_type)
    info.model = data.get("model_name") or data.get("product")
    info.serial = data.get("serial_number")
    info.firmware = data.get("firmware_version")
    info.capacity = (data.get("user_capacity") or {}).get("bytes")
    info.rotation_rate = data.get("rotation_rate")
    info.power_on_hours = (data.get("power_on_time") or {}).get("hours")
    info.temperature = (data.get("temperature") or {}).get("current")
    status = data.get("smart_status")
    if isinstance(status, dict) and "passed" in status:
        info.passed = bool(status["passed"])
    for attr in (data.get("ata_smart_attributes") or {}).get("table", []):
        raw = (attr.get("raw") or {}).get("value")
        if isinstance(attr.get("id"), int) and isinstance(raw, int):
            # Some attributes pack extra data into the high bytes; keep the low 32 bits.
            info.attributes[attr["id"]] = (attr.get("name", str(attr["id"])), raw & 0xFFFFFFFF)
    return info


def _useful(info: SmartInfo) -> bool:
    return bool(info.serial or info.attributes or info.passed is not None)


def query(device: Path) -> SmartInfo:
    smartctl = tools.find_tool("smartctl")
    if smartctl is None:
        return SmartInfo(available=False, error="smartctl not installed (brew install smartmontools)")
    last_error = "no usable SMART data"
    for dtype in DEVICE_TYPES:
        args = [smartctl, "-j", "-i", "-H", "-A"]
        if dtype:
            args += ["-d", dtype]
        proc = tools.run([*args, str(device)], check=False)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            last_error = (proc.stderr or proc.stdout).strip()[:200] or last_error
            continue
        # Exit-status bits 0/1: command line error / device open failed.
        if proc.returncode & 0b11:
            messages = (data.get("smartctl") or {}).get("messages") or []
            if messages:
                last_error = messages[0].get("string", last_error)
            continue
        info = parse(data, dtype)
        if _useful(info):
            return info
    return SmartInfo(available=False, error=last_error)


def compare(before: SmartInfo, after: SmartInfo) -> list[SmartChange]:
    changes = []
    for attr_id in sorted({*CRITICAL, *LINK}):
        b, a = before.raw(attr_id), after.raw(attr_id)
        if b is None or a is None or a <= b:
            continue
        name = CRITICAL.get(attr_id) or LINK[attr_id]
        changes.append(SmartChange(attr_id, name, b, a, critical=attr_id in CRITICAL))
    return changes


def existing_problems(info: SmartInfo) -> list[str]:
    """Pre-existing red flags worth showing before the test even starts."""
    problems = []
    if info.passed is False:
        problems.append("SMART overall health self-assessment: FAILED")
    for attr_id, name in CRITICAL.items():
        value = info.raw(attr_id)
        if value:
            problems.append(f"{name} (SMART {attr_id}) already at {value}")
    return problems
