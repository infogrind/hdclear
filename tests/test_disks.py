from pathlib import Path

import pytest
from conftest import add_sys_disk

from hdclear import disks
from hdclear.errors import HdclearError


def test_read_usb_disk(sys_root):
    add_sys_disk(sys_root, partitions={"sdq1": 1000, "sdq2": 2000})

    disk = disks.read_disk("sdq", sys_root, Path("/dev"))

    assert disk.path == Path("/dev/sdq")
    assert disk.size == 15628053168 * 512
    assert disk.transport == "usb"
    assert disk.description == "ST8000DM 004-2CX188"
    assert disk.usb_serial == "QP0000FAKE01"
    assert disk.physical_block_size == 4096
    assert [p.name for p in disk.partitions] == ["sdq1", "sdq2"]
    assert disk.partitions[1].size == 2000 * 512


def test_sata_disk_has_no_usb_serial(sys_root):
    add_sys_disk(sys_root, "sda", transport="sata")

    disk = disks.read_disk("sda", sys_root)

    assert disk.transport == "sata"
    assert disk.usb_serial is None


def test_holders_are_read(sys_root):
    add_sys_disk(sys_root, partitions={"sdq1": 10}, holders=["dm-0"], part_holders={"sdq1": ["md2"]})

    disk = disks.read_disk("sdq", sys_root)

    assert disk.holders == ["dm-0"]
    assert disk.partitions[0].holders == ["md2"]


def test_list_disks_skips_virtual_and_empty(sys_root):
    add_sys_disk(sys_root, "sdq")
    add_sys_disk(sys_root, "loop0", transport="virtual")
    add_sys_disk(sys_root, "sdr", sectors=0)  # card reader without card

    assert [d.name for d in disks.list_disks(sys_root)] == ["sdq"]


def test_resolve_device_accepts_dev_path_and_bare_name(sys_root):
    add_sys_disk(sys_root, partitions={"sdq1": 10})

    assert disks.resolve_device("sdq", sys_root) == "sdq"
    assert disks.resolve_device("/dev/sdq", sys_root) == "sdq"


def test_resolve_device_rejects_partition(sys_root):
    add_sys_disk(sys_root, partitions={"sdq1": 10})

    with pytest.raises(HdclearError, match="is a partition"):
        disks.resolve_device("/dev/sdq1", sys_root)


def test_resolve_device_rejects_unknown(sys_root):
    with pytest.raises(HdclearError, match="no such disk"):
        disks.resolve_device("/dev/sdz", sys_root)


def test_read_system_state(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "mounts").write_text(
        "/dev/md0 / ext4 rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "/dev/sdq1 /volumeUSB1/usbshare ext4 rw 0 0\n"
        "/dev/sdr1 /mnt/my\\040disk vfat rw 0 0\n"
    )
    (proc / "swaps").write_text(
        "Filename Type Size Used Priority\n/dev/md1 partition 2097084 0 -2\n"
    )
    (proc / "mdstat").write_text(
        "Personalities : [raid1]\n"
        "md2 : active raid1 sata1p3[0] sata2p3[1]\n"
        "      3897366528 blocks super 1.2 [2/2] [UU]\n"
        "md0 : active raid1 sata1p1[0]\n"
    )

    state = disks.read_system_state(proc)

    assert [(m.device, m.mountpoint) for m in state.mounts] == [
        ("md0", "/"),
        ("sdq1", "/volumeUSB1/usbshare"),
        ("sdr1", "/mnt/my disk"),
    ]
    assert state.swaps == {"md1"}
    assert state.md_members == {"sata1p3", "sata2p3", "sata1p1"}


@pytest.mark.parametrize(
    ("disk", "expected"), [("sdq", "sdq1"), ("nvme0n1", "nvme0n1p1"), ("sata1", "sata1p1")]
)
def test_partition_device_name(disk, expected):
    assert disks.partition_device_name(disk, 1) == expected
