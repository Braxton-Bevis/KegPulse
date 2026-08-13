from __future__ import annotations

import errno
import importlib
import os
from pathlib import Path
from types import TracebackType


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another process holds the data-directory lock."""


class InstanceLock:
    """An OS-held, non-blocking lock tied to one KegPulse data directory.

    The lock file is deliberately retained after release. Ownership is established by
    the kernel lock, not by file existence, so an unclean process exit cannot leave a
    stale lock that blocks the next launch.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("instance lock is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            if os.name != "nt":
                self.path.chmod(0o600)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            self._acquire_os_lock(fd)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    @staticmethod
    def _acquire_os_lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise InstanceAlreadyRunning("data directory is already in use") from exc
            return

        fcntl = importlib.import_module("fcntl")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise InstanceAlreadyRunning("data directory is already in use") from exc
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
