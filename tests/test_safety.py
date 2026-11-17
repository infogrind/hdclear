from pathlib import Path

from conftest import make_disk

from hdclear.disks import Mount, Partition, SystemState
from hdclear.safety import check_disk


def state(mounts=(), swaps=(), md=()):
    return SystemState(list(mounts), set(swaps), set(md))


def test_clean_usb_disk_is_ok():
    report = check_disk(make_disk(), state())
    assert report.ok
    assert report.warnings == []


def test_internal_disk_is_blocked_unless_allowed():
    disk = make_disk(transport="sata")

    assert "not USB" in check_disk(disk, state()).blockers[0]

    allowed = check_disk(disk, state(), allow_internal=True)
    assert allowed.ok
    assert "--allow-internal" in allowed.warnings[0]


def test_raid_member_is_blocked_even_with_allow_internal():
    disk = make_disk(
        name="sata1",
        transport="sata",
        partitions=[Partition("sata1p3", Path("/dev/sata1p3"), 10, holders=["md2"])],
    )

    report = check_disk(disk, state(md={"sata1p3"}), allow_internal=True)

    assert not report.ok
    assert any("md2" in b for b in report.blockers)
    assert any("software RAID" in b for b in report.blockers)


def test_usb_mount_is_a_warning_but_system_mount_blocks():
    usb = check_disk(make_disk(), state([Mount("sdq1", "/volumeUSB1/usbshare", "ext4")]))
    assert usb.ok
    assert "will be unmounted" in usb.warnings[0]

    system = check_disk(make_disk(), state([Mount("sdq1", "/volume1", "btrfs")]))
    assert not system.ok
    assert "system path" in system.blockers[0]


def test_swap_and_holders_block():
    report = check_disk(make_disk(holders=["dm-3"]), state(swaps={"sdq1"}))
    assert len(report.blockers) == 2


def test_zero_size_blocks():
    assert not check_disk(make_disk(size=0, partitions=[]), state()).ok


def test_container_manager_bind_mount_of_usb_share_is_a_warning():
    # DSM 7.2 names USB disks usbN and Container Manager re-mounts their shares.
    disk = make_disk(name="usb2", partitions=[Partition("usb2p1", Path("/dev/usb2p1"), 10)])
    mounts = [
        Mount("usb2p1", "/volumeUSB2/usbshare", "ext4"),
        Mount("usb2p1", "/volume1/@appdata/ContainerManager/all_shares/usbshare1", "ext4"),
    ]

    report = check_disk(disk, state(mounts))

    assert report.ok
    assert "second mount" in report.warnings[1]


def test_second_mount_on_system_path_still_blocks():
    mounts = [Mount("sdq1", "/volumeUSB1/usbshare", "ext4"), Mount("sdq1", "/volume1", "ext4")]
    assert not check_disk(make_disk(), state(mounts)).ok


def test_non_removable_mount_without_usb_mount_blocks():
    mounts = [Mount("sdq1", "/volume1/@appdata/ContainerManager/all_shares/usbshare1", "ext4")]
    assert not check_disk(make_disk(), state(mounts)).ok
