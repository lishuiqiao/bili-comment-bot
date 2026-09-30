"""Local filesystem lifecycle lock shared by auth commands and the service runner."""

import fcntl
import os
import stat
from pathlib import Path


class InstanceInUse(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.descriptor: int | None = None

    def __enter__(self):
        if self.descriptor is not None:
            raise RuntimeError("lock is already held")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(self.directory / ".instance.lock", flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PermissionError("lock must be a regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstanceInUse("此数据目录正在使用") from None
            os.chmod(self.directory, 0o700)
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc, traceback):
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        # Keep the inode: deleting a lock file permits two independently locked inodes.
