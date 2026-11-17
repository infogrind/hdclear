"""Surface test against plain files standing in for block devices."""

import errno
import os

import pytest

from hdclear import surface
from hdclear.surface import DiskGoneError, SurfaceTest, TooManyErrorsError

SECTOR = 4096
CHUNK = 64 * 1024


def make_file(tmp_path, size):
    path = tmp_path / "disk.img"
    path.write_bytes(b"\xaa" * size)
    return path


def new_test(path, size, **kw):
    kw.setdefault("sector", SECTOR)
    kw.setdefault("chunk", CHUNK)
    kw.setdefault("direct", False)
    return SurfaceTest(path, size, **kw)


def test_good_disk_passes_and_reports_progress(tmp_path):
    size = 5 * CHUNK + 3 * SECTOR  # last chunk is partial
    path = make_file(tmp_path, size)
    calls = []
    test = new_test(path, size, progress=lambda *a: calls.append(a))

    w = test.write_pass()
    r = test.read_pass()

    assert w.bad == [] and r.bad == []
    assert w.bytes_done == r.bytes_done == size
    assert [c[1] for c in calls if c[0] == "write"][-1] == size
    assert [c[1] for c in calls if c[0] == "verify"][-1] == size
    # Every chunk carries its own offset, so no two chunks are identical.
    data = path.read_bytes()
    assert data[:CHUNK] != data[CHUNK : 2 * CHUNK]
    assert data[CHUNK : CHUNK + 8] == CHUNK.to_bytes(8, "little")


def test_corruption_is_located_to_the_sector(tmp_path):
    size = 4 * CHUNK
    path = make_file(tmp_path, size)
    test = new_test(path, size)
    test.write_pass()

    bad_offset = CHUNK + 5 * SECTOR + 100
    with path.open("r+b") as f:
        f.seek(bad_offset)
        f.write(b"\x00\x01\x02")

    result = test.read_pass()

    assert len(result.bad) == 1
    bad = result.bad[0]
    assert (bad.offset, bad.length, bad.detail) == (CHUNK + 5 * SECTOR, SECTOR, "data mismatch")
    assert test.bad_sectors == 1


def test_aliasing_disk_is_detected(tmp_path):
    # A "fake capacity" disk that wraps: the second half mirrors the first.
    size = 4 * CHUNK
    path = make_file(tmp_path, size)
    test = new_test(path, size)
    test.write_pass()
    data = path.read_bytes()
    path.write_bytes(data[: size // 2] * 2)

    result = test.read_pass()

    assert sum(r.length for r in result.bad) >= 2 * SECTOR  # every wrapped chunk header mismatches


def test_read_errors_are_narrowed_down(tmp_path, monkeypatch):
    size = 4 * CHUNK
    path = make_file(tmp_path, size)
    test = new_test(path, size)
    test.write_pass()
    bad_sector = 2 * CHUNK + 3 * SECTOR
    real_preadv = os.preadv

    def flaky_preadv(fd, buffers, offset):
        length = sum(len(b) for b in buffers)
        if offset <= bad_sector < offset + length:
            raise OSError(errno.EIO, "Input/output error")
        return real_preadv(fd, buffers, offset)

    monkeypatch.setattr(surface.os, "preadv", flaky_preadv)
    result = test.read_pass()

    assert [(r.offset, r.length) for r in result.bad] == [(bad_sector, SECTOR)]
    assert result.bad[0].detail.startswith("read error")


def test_write_errors_coalesce_adjacent_sectors(tmp_path, monkeypatch):
    size = 2 * CHUNK
    path = make_file(tmp_path, size)
    test = new_test(path, size)
    bad = range(SECTOR * 2, SECTOR * 4)
    real_pwrite = os.pwrite

    def flaky_pwrite(fd, data, offset):
        if any(o in bad for o in range(offset, offset + len(data), SECTOR)):
            raise OSError(errno.EIO, "Input/output error")
        return real_pwrite(fd, data, offset)

    monkeypatch.setattr(surface.os, "pwrite", flaky_pwrite)
    result = test.write_pass()

    assert [(r.offset, r.length) for r in result.bad] == [(2 * SECTOR, 2 * SECTOR)]
    assert test.bad_sectors == 2


def test_gives_up_after_too_many_bad_sectors(tmp_path, monkeypatch):
    size = 2 * CHUNK
    path = make_file(tmp_path, size)
    test = new_test(path, size, max_bad_sectors=3)

    def dead_pwrite(fd, data, offset):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(surface.os, "pwrite", dead_pwrite)
    with pytest.raises(TooManyErrorsError):
        test.write_pass()


def test_disconnected_disk_aborts(tmp_path, monkeypatch):
    size = 2 * CHUNK
    path = make_file(tmp_path, size)
    test = new_test(path, size)

    def gone(fd, data, offset):
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(surface.os, "pwrite", gone)
    with pytest.raises(DiskGoneError, match="disconnected"):
        test.write_pass()


def test_rejects_misaligned_chunk(tmp_path):
    with pytest.raises(ValueError):
        SurfaceTest(tmp_path / "x", 4096, sector=4096, chunk=4096 + 512)
