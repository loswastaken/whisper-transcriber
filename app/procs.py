"""Child processes that can be stopped by a cancel flag or a hard timeout (stdlib only).

Every child starts in its own session, so its process group id equals its pid and
stopping it signals only that group: the child and anything it started (for
example the ffmpeg that yt-dlp runs). The leader is reaped only after the last
group signal, so the group id cannot be reused by an unrelated process meanwhile.
Nothing is ever killed by name or pattern.

Seeing the leader exit without reaping it: waitid(WNOWAIT) on Linux, a kqueue
NOTE_EXIT watch on macOS (which has no waitid). Without either, Popen.poll() and
wait() are the last resort; they do reap the leader, so the final group signal
could in principle reach a new group that reused the id.
"""

import errno
import logging
import os
import select
import signal
import subprocess
import threading
import time
from typing import Optional, Sequence

TERM_GRACE_SECONDS = 3.0
POLL_SECONDS = 0.2
KQUEUE_WAIT_SECONDS = 1.0

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    """The user cancelled the job. Never reported as an error."""


def check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelled()


class Child:
    """Popen in a new session, watched by a thread that stops its group when the
    cancel flag is set (reason "cancelled") or the timeout passes ("timeout").

    Use as a context manager; call wait() for the exit code. Leaving the block
    without wait() (an exception) stops the group too.
    """

    def __init__(self, cmd: Sequence[str], cancel: Optional[threading.Event] = None,
                 timeout: Optional[float] = None, **popen_kwargs):
        popen_kwargs.setdefault("stdin", subprocess.DEVNULL)
        self.cancel = cancel
        self.reason: Optional[str] = None
        self._deadline = None if timeout is None else time.monotonic() + timeout
        self._done = threading.Event()
        self._kqueue_failed = False
        self.proc = subprocess.Popen(list(cmd), start_new_session=True, **popen_kwargs)
        self._watcher = threading.Thread(target=self._watch, name="child-watch", daemon=True)
        self._watcher.start()

    def __enter__(self) -> "Child":
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.proc.returncode is None:
                self._done.set()
                self._watcher.join()
                self._stop_group()
                self.proc.wait()
        finally:
            for f in (self.proc.stdout, self.proc.stderr):
                if f is not None:
                    f.close()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def wait(self) -> int:
        """Wait for the child to exit, clear any leftovers in its group, reap it."""
        self._wait_exit(block=True)
        self._done.set()
        self._watcher.join()
        self._signal(signal.SIGKILL)
        return self.proc.wait()

    def _watch(self) -> None:
        while True:
            if self.cancel is not None and self.cancel.is_set():
                self.reason = "cancelled"
            elif self._deadline is not None and time.monotonic() >= self._deadline:
                self.reason = "timeout"
            elif self._done.wait(POLL_SECONDS):
                return
            else:
                continue
            self._stop_group()
            return

    def _wait_exit(self, block: bool) -> bool:
        """True once the leader has exited. Does not reap it, except in the
        last-resort fallback (see the module docstring)."""
        if hasattr(os, "waitid"):
            flags = os.WEXITED | os.WNOWAIT | (0 if block else os.WNOHANG)
            try:
                return os.waitid(os.P_PID, self.proc.pid, flags) is not None
            except ChildProcessError:
                return True
        if hasattr(select, "kqueue") and not self._kqueue_failed:
            try:
                # Bounded waits with a fresh registration each time, so a missed
                # event cannot block forever: re-registering fails once it exited.
                while not _kqueue_exited(self.proc.pid, KQUEUE_WAIT_SECONDS if block else 0):
                    if not block:
                        return False
                return True
            except OSError:
                log.warning("kqueue exit watch failed; falling back to polling", exc_info=True)
                self._kqueue_failed = True
        return self._poll_exit(block)

    def _poll_exit(self, block: bool) -> bool:
        """Last resort without waitid or kqueue. Reaps the leader."""
        if block:
            self.proc.wait()
            return True
        return self.proc.poll() is not None

    def _stop_group(self) -> None:
        self._signal(signal.SIGTERM)
        end = time.monotonic() + TERM_GRACE_SECONDS
        while not self._wait_exit(block=False) and time.monotonic() < end:
            time.sleep(0.05)
        self._signal(signal.SIGKILL)

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _kqueue_exited(pid: int, timeout: float) -> bool:
    """macOS/BSD: True if pid exits within timeout seconds (0 = just check).
    Does not reap it.

    Registering an EVFILT_PROC watch fails with ESRCH once the process has exited
    (zombies included) or is gone, and NOTE_EXIT fires for an exit after
    registration, so together they leave no gap. Other errors raise OSError.
    """
    kq = select.kqueue()
    try:
        watch = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                              flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)
        try:
            kq.control([watch], 0, 0)  # no room for events: errors raise
        except ProcessLookupError:
            return True
        events = kq.control(None, 1, timeout)
        for event in events:
            if event.flags & select.KQ_EV_ERROR and event.data != errno.ESRCH:
                raise OSError(event.data, os.strerror(event.data))
        return bool(events)
    finally:
        kq.close()
