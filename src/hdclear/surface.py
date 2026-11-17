"""The surface test: write a pattern over the whole disk, then read it all back.

Design notes:

* Every chunk starts with a 16-byte header holding its own byte offset and a
  per-run tag, the rest is a random pattern generated once per run. Because
  each chunk is unique, the verify pass also catches address aliasing (a
  "fake capacity" disk that wraps around) and stale data from an earlier run.
* I/O uses O_DIRECT where possible (page-aligned mmap buffers), so the verify
  pass reads the platters, not the page cache. If O_DIRECT is refused we fall
  back to buffered I/O plus fsync and posix_fadvise(DONTNEED).
* An I/O error on a chunk is narrowed down by retrying sector by sector, so
  the report lists actual bad sector ranges instead of whole chunks.
"""

from __future__ import annotations

import errno
import mmap
import os
import secrets
import stat
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import HdclearError

DEFAULT_CHUNK = 8 * 1024 * 1024
HEADER = struct.Struct("<QQ")
# These mean the device itself went away -- pointless to continue.
GONE_ERRNOS = {errno.ENODEV, errno.ENXIO, errno.ENOENT, errno.ESHUTDOWN}


def _release(view: memoryview, buf: mmap.mmap) -> None:
    # If an exception is propagating, its traceback may still reference slices
    # of the buffer; closing would then raise BufferError and mask the real
    # error. Leave the mapping to the garbage collector in that case.
    try:
        view.release()
        buf.close()
    except BufferError:
        pass


class DiskGoneError(HdclearError):
    pass


class TooManyErrorsError(HdclearError):
    pass


@dataclass
class BadRange:
    phase: str  # "write" | "read" | "verify"
    offset: int
    length: int
    detail: str


@dataclass
class PhaseResult:
    phase: str
    bytes_done: int = 0
    seconds: float = 0.0
    bad: list[BadRange] = field(default_factory=list)

    @property
    def bad_bytes(self) -> int:
        return sum(r.length for r in self.bad)

    @property
    def mb_per_s(self) -> float:
        return self.bytes_done / self.seconds / 1e6 if self.seconds > 0 else 0.0


# Called as progress(phase, offset_done, chunk_bytes, chunk_seconds, chunk_had_error).
ProgressFn = Callable[[str, int, int, float, bool], None]


class SurfaceTest:
    def __init__(
        self,
        path: Path,
        size: int,
        *,
        sector: int = 4096,
        chunk: int = DEFAULT_CHUNK,
        max_bad_sectors: int = 256,
        direct: bool = True,
        progress: ProgressFn | None = None,
    ):
        if size <= 0:
            raise HdclearError("cannot test a zero-size device")
        if chunk % sector or chunk % mmap.PAGESIZE:
            raise ValueError("chunk must be a multiple of the sector and page size")
        self.path = path
        self.size = size
        self.sector = sector
        self.chunk = chunk
        self.max_bad_sectors = max_bad_sectors
        self.want_direct = direct
        self.progress = progress
        self.run_tag = secrets.randbits(64)
        self.pattern = secrets.token_bytes(chunk)
        self.bad_sectors = 0
        self.direct = False

    # -- buffers & file handling ------------------------------------------

    def expected(self, offset: int, length: int) -> bytes:
        return HEADER.pack(offset, self.run_tag) + self.pattern[HEADER.size : length]

    def _open(self, write: bool) -> int:
        flags = os.O_RDWR if write else os.O_RDONLY
        if os.path.exists(self.path) and stat.S_ISBLK(os.stat(self.path).st_mode):
            # On Linux, O_EXCL on a block device fails if anything has it mounted.
            flags |= os.O_EXCL
        if self.want_direct and hasattr(os, "O_DIRECT"):
            try:
                fd = os.open(self.path, flags | os.O_DIRECT)
                self.direct = True
                return fd
            except OSError as e:
                if e.errno != errno.EINVAL:
                    raise self._open_error(e) from e
        try:
            fd = os.open(self.path, flags)
        except OSError as e:
            raise self._open_error(e) from e
        self.direct = False
        return fd

    def _open_error(self, e: OSError) -> HdclearError:
        if e.errno == errno.EBUSY:
            return HdclearError(f"{self.path} is busy (still mounted or in use)")
        return HdclearError(f"cannot open {self.path}: {e.strerror}")

    def _drop_cache(self, fd: int) -> None:
        if not self.direct and hasattr(os, "posix_fadvise"):
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass

    # -- error bookkeeping ------------------------------------------------

    def _record(self, result: PhaseResult, offset: int, length: int, detail: str) -> None:
        last = result.bad[-1] if result.bad else None
        if last and last.offset + last.length == offset and last.detail == detail:
            last.length += length
        else:
            result.bad.append(BadRange(result.phase, offset, length, detail))
        self.bad_sectors += max(1, length // self.sector)
        if self.bad_sectors > self.max_bad_sectors:
            raise TooManyErrorsError(
                f"more than {self.max_bad_sectors} bad sectors found - giving up, this disk is not usable"
            )

    @staticmethod
    def _check_gone(e: OSError) -> None:
        if e.errno in GONE_ERRNOS:
            raise DiskGoneError(f"the disk disappeared ({e.strerror}) - was it disconnected?") from e

    # -- low-level I/O ----------------------------------------------------

    @staticmethod
    def _pwrite_all(fd: int, view: memoryview, offset: int) -> None:
        done = 0
        while done < len(view):
            n = os.pwrite(fd, view[done:], offset + done)
            if n <= 0:
                raise OSError(errno.EIO, "short write")
            done += n

    @staticmethod
    def _pread_all(fd: int, view: memoryview, offset: int) -> None:
        done = 0
        while done < len(view):
            n = os.preadv(fd, [view[done:]], offset + done)
            if n <= 0:
                raise OSError(errno.EIO, "unexpected end of device")
            done += n

    # -- phases -----------------------------------------------------------

    def write_pass(self) -> PhaseResult:
        result = PhaseResult("write")
        fd = self._open(write=True)
        buf = mmap.mmap(-1, self.chunk)
        view = memoryview(buf)
        try:
            offset = 0
            start = time.monotonic()
            while offset < self.size:
                length = min(self.chunk, self.size - offset)
                buf[:length] = self.expected(offset, length)
                t0 = time.monotonic()
                had_error = False
                try:
                    self._pwrite_all(fd, view[:length], offset)
                except OSError as e:
                    self._check_gone(e)
                    had_error = True
                    self._write_sectors(fd, view, offset, length, result)
                result.bytes_done = offset + length
                self._report("write", offset + length, length, time.monotonic() - t0, had_error)
                offset += length
            try:
                os.fsync(fd)
            except OSError as e:
                self._check_gone(e)
                self._record(result, 0, 0, f"flush failed: {e.strerror}")
            result.seconds = time.monotonic() - start
        finally:
            _release(view, buf)
            self._drop_cache(fd)
            os.close(fd)
        return result

    def _write_sectors(
        self, fd: int, view: memoryview, offset: int, length: int, result: PhaseResult
    ) -> None:
        for pos in range(0, length, self.sector):
            n = min(self.sector, length - pos)
            try:
                self._pwrite_all(fd, view[pos : pos + n], offset + pos)
            except OSError as e:
                self._check_gone(e)
                self._record(result, offset + pos, n, f"write error: {e.strerror}")

    def read_pass(self) -> PhaseResult:
        result = PhaseResult("verify")
        fd = self._open(write=False)
        self._drop_cache(fd)
        buf = mmap.mmap(-1, self.chunk)
        view = memoryview(buf)
        try:
            offset = 0
            start = time.monotonic()
            while offset < self.size:
                length = min(self.chunk, self.size - offset)
                t0 = time.monotonic()
                had_error = False
                try:
                    self._pread_all(fd, view[:length], offset)
                    if buf[:length] != self.expected(offset, length):
                        had_error = True
                        self._verify_sectors(buf, offset, length, result)
                except OSError as e:
                    self._check_gone(e)
                    had_error = True
                    self._read_sectors(fd, view, buf, offset, length, result)
                result.bytes_done = offset + length
                if not self.direct and (offset // self.chunk) % 128 == 0:
                    self._drop_cache(fd)
                self._report("verify", offset + length, length, time.monotonic() - t0, had_error)
                offset += length
            result.seconds = time.monotonic() - start
        finally:
            _release(view, buf)
            os.close(fd)
        return result

    def _verify_sectors(self, buf: mmap.mmap, offset: int, length: int, result: PhaseResult) -> None:
        expected = self.expected(offset, length)
        for pos in range(0, length, self.sector):
            end = min(pos + self.sector, length)
            if buf[pos:end] != expected[pos:end]:
                self._record(result, offset + pos, end - pos, "data mismatch")

    def _read_sectors(
        self, fd: int, view: memoryview, buf: mmap.mmap, offset: int, length: int, result: PhaseResult
    ) -> None:
        expected = self.expected(offset, length)
        for pos in range(0, length, self.sector):
            end = min(pos + self.sector, length)
            try:
                self._pread_all(fd, view[pos:end], offset + pos)
            except OSError as e:
                self._check_gone(e)
                self._record(result, offset + pos, end - pos, f"read error: {e.strerror}")
                continue
            if buf[pos:end] != expected[pos:end]:
                self._record(result, offset + pos, end - pos, "data mismatch")

    def _report(self, phase: str, done: int, length: int, seconds: float, had_error: bool) -> None:
        if self.progress is not None:
            self.progress(phase, done, length, seconds, had_error)
