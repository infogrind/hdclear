import subprocess

from hdclear import formatter, tools
from hdclear.disks import Mount


def test_unmount_does_deepest_mount_first(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "require_tool", lambda name: name)

    def run(args, *, input=None, check=True):
        calls.append(args[1])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tools, "run", run)
    formatter.unmount(
        [
            Mount("usb2p1", "/volumeUSB2/usbshare", "ext4"),
            Mount("usb2p1", "/volume1/@appdata/ContainerManager/all_shares/usbshare1", "ext4"),
        ]
    )

    assert calls == [
        "/volume1/@appdata/ContainerManager/all_shares/usbshare1",
        "/volumeUSB2/usbshare",
    ]
