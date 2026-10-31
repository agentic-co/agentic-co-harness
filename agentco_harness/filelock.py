"""Cross-platform advisory file locking.

`fcntl` is POSIX-only and was imported at module scope in five modules
(beads, asop_store, natural_key, recurring, schedules), so the CLI failed at
import on native Windows and the documented workaround was WSL2. This is the
hub's `agentco/filelock.py`, the same fix the hub made for the same reason.

The lock is what makes the read-modify-write cycle atomic across processes, so
this is not somewhere to degrade quietly: on a platform with no implementation
the caller is told, rather than running unlocked and losing writes under
concurrency that the whole design assumes is happening.
"""

from __future__ import annotations

import sys
from typing import IO

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def lock_exclusive(handle: IO) -> None:
        # Byte-range lock on the first byte. The lock FILE is a sidecar holding
        # no data, so locking one byte of it locks the resource entirely.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def unlock(handle: IO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def lock_exclusive(handle: IO) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX)

    def unlock(handle: IO) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)
