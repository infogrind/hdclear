import json
import subprocess
from pathlib import Path

from hdclear import smart, tools


def smartctl_json(**overrides):
    data = {
        "smartctl": {"exit_status": 0},
        "model_name": "ST8000DM004-2CX188",
        "serial_number": "ZCT0FAKE",
        "firmware_version": "0001",
        "user_capacity": {"bytes": 8001563222016},
        "rotation_rate": 5425,
        "power_on_time": {"hours": 17532},
        "temperature": {"current": 34},
        "smart_status": {"passed": True},
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0}},
                {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 0}},
                {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 2}},
                {"id": 194, "name": "Temperature_Celsius", "raw": {"value": 0x2200000022}},
            ]
        },
    }
    data.update(overrides)
    return data


def test_parse():
    info = smart.parse(smartctl_json(), "sat")

    assert info.available and info.device_type == "sat"
    assert info.serial == "ZCT0FAKE"
    assert info.power_on_hours == 17532
    assert info.passed is True
    assert info.raw(199) == 2
    assert info.raw(194) == 0x22  # high bytes masked off


def fake_run(responses):
    """responses: list of (returncode, stdout) consumed in order; records args."""
    calls = []

    def run(args, *, input=None, check=True):
        calls.append(args)
        code, out = responses.pop(0)
        return subprocess.CompletedProcess(args, code, out, "")

    return run, calls


def test_query_falls_back_to_sat(monkeypatch):
    monkeypatch.setattr(tools, "find_tool", lambda name: "/usr/sbin/smartctl")
    bridge_error = {"smartctl": {"messages": [{"string": "Unknown USB bridge"}]}}
    run, calls = fake_run([(1, json.dumps(bridge_error)), (0, json.dumps(smartctl_json()))])
    monkeypatch.setattr(tools, "run", run)

    info = smart.query(Path("/dev/sdq"))

    assert info.available and info.device_type == "sat"
    assert calls[1][-3:] == ["-d", "sat", "/dev/sdq"]


def test_query_reports_last_error_when_nothing_works(monkeypatch):
    monkeypatch.setattr(tools, "find_tool", lambda name: "/usr/sbin/smartctl")
    bridge_error = {"smartctl": {"messages": [{"string": "Unknown USB bridge"}]}}
    run, _ = fake_run([(2, json.dumps(bridge_error))] * 3)
    monkeypatch.setattr(tools, "run", run)

    info = smart.query(Path("/dev/sdq"))

    assert not info.available
    assert info.error == "Unknown USB bridge"


def test_query_without_smartctl(monkeypatch):
    monkeypatch.setattr(tools, "find_tool", lambda name: None)
    assert "smartmontools" in smart.query(Path("/dev/sdq")).error


def with_attrs(**raw):
    table = [{"id": int(k[1:]), "name": k, "raw": {"value": v}} for k, v in raw.items()]
    return smart.parse(smartctl_json(ata_smart_attributes={"table": table}), None)


def test_compare_flags_critical_and_link_changes():
    before = with_attrs(a5=0, a197=0, a199=2, a9=100)
    after = with_attrs(a5=8, a197=0, a199=3, a9=130)

    changes = smart.compare(before, after)

    assert [(c.attr_id, c.before, c.after, c.critical) for c in changes] == [
        (5, 0, 8, True),
        (199, 2, 3, False),
    ]


def test_existing_problems():
    info = with_attrs(a5=0, a197=16)
    info.passed = False
    problems = smart.existing_problems(info)
    assert any("FAILED" in p for p in problems)
    assert any("Pending sectors" in p and "16" in p for p in problems)
