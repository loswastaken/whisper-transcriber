"""procs.Child without os.waitid, as on macOS, with real child processes.

Linux has no kqueue, so two stand-ins run here: the polling fallback (no waitid,
no kqueue) and a fake `select` module whose kqueue follows the kevent(2)
contract (ESRCH on registering an exited pid, a NOTE_EXIT event otherwise),
answering from the real os.waitid(WNOWAIT) so it never reaps anything. The job
cancel suite in test_cancel.py is run again under both.
"""

import errno
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

from app import procs
from tests import test_cancel
from tests.test_cancel import group_alive

LINUX = os.path.isdir("/proc") and hasattr(os, "waitid") and hasattr(os, "killpg")


class OsWithout:
    """The os module minus some names (hasattr() is False for them)."""

    def __init__(self, *hidden):
        self._hidden = hidden

    def __getattr__(self, name):
        if name in self._hidden:
            raise AttributeError(name)
        return getattr(os, name)


def leader_exited(pid):
    """Real check that never reaps: True for a zombie or a pid already reaped."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
    except ChildProcessError:
        return True


class FakeKevent:
    def __init__(self, ident, filter=0, flags=0, fflags=0, data=0, udata=0):
        self.ident, self.filter, self.flags = ident, filter, flags
        self.fflags, self.data, self.udata = fflags, data, udata


def fake_select():
    stats = {"opened": 0, "closed": 0, "registered": 0, "esrch": 0}
    mod = types.SimpleNamespace(KQ_FILTER_PROC=-5, KQ_NOTE_EXIT=0x80000000,
                                KQ_EV_ADD=0x1, KQ_EV_ERROR=0x4000, kevent=FakeKevent,
                                stats=stats)

    class FakeKqueue:
        def __init__(self):
            stats["opened"] += 1
            self.pid = None
            self.closed = False

        def control(self, changelist, max_events, timeout=None):
            assert not self.closed
            if changelist:
                (kev,) = changelist
                assert kev.filter == mod.KQ_FILTER_PROC and kev.flags & mod.KQ_EV_ADD
                assert kev.fflags & mod.KQ_NOTE_EXIT
                assert max_events == 0  # so a registration error raises
                if leader_exited(kev.ident):
                    stats["esrch"] += 1
                    raise ProcessLookupError(errno.ESRCH, "No such process")
                stats["registered"] += 1
                self.pid = kev.ident
                return []
            assert self.pid is not None and max_events >= 1
            end = None if timeout is None else time.monotonic() + timeout
            while True:
                if leader_exited(self.pid):
                    return [FakeKevent(self.pid, mod.KQ_FILTER_PROC, 0, mod.KQ_NOTE_EXIT)]
                if end is not None and time.monotonic() >= end:
                    return []
                time.sleep(0.01)

        def close(self):
            self.closed = True
            stats["closed"] += 1

    mod.kqueue = FakeKqueue
    return mod


def sh(script):
    return ["/bin/sh", "-c", script]


class ChildChecks:
    """Child behaviour that must hold whichever exit check is in use."""

    def patch_exit_check(self):
        raise NotImplementedError

    def setUp(self):
        self.patch_exit_check()

    def test_exit_code(self):
        with procs.Child(sh("sleep 0.2; exit 3")) as child:
            self.assertEqual(child.wait(), 3)
        self.assertIsNone(child.reason)

    def test_already_exited_before_wait(self):
        with procs.Child(["true"]) as child:
            time.sleep(0.3)
            self.assertEqual(child.wait(), 0)

    def test_cancel_stops_whole_group(self):
        cancel = threading.Event()
        t0 = time.monotonic()
        with procs.Child(sh("sleep 30 & sleep 30"), cancel=cancel) as child:
            pgid = child.pid
            time.sleep(0.3)
            cancel.set()
            child.wait()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(child.reason, "cancelled")
        self.assertFalse(group_alive(pgid))

    def test_sigterm_ignored_gets_sigkill(self):
        cancel = threading.Event()
        script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        with procs.Child([sys.executable, "-c", script], cancel=cancel) as child:
            pgid = child.pid
            time.sleep(0.3)
            cancel.set()
            rc = child.wait()
        self.assertEqual(rc, -9)
        self.assertFalse(group_alive(pgid))

    def test_timeout(self):
        with procs.Child(["sleep", "30"], timeout=0.3) as child:
            child.wait()
        self.assertEqual(child.reason, "timeout")

    def test_leaving_block_without_wait_stops_group(self):
        with self.assertRaises(RuntimeError):
            with procs.Child(sh("sleep 30 & sleep 30")) as child:
                pgid = child.pid
                raise RuntimeError("boom")
        self.assertFalse(group_alive(pgid))
        self.assertIsNotNone(child.proc.returncode)


@unittest.skipUnless(LINUX, "needs Linux")
class PollFallbackTest(ChildChecks, unittest.TestCase):
    def patch_exit_check(self):
        for p in (mock.patch.object(procs, "os", OsWithout("waitid")),
                  mock.patch.object(procs, "select", types.SimpleNamespace())):
            p.start()
            self.addCleanup(p.stop)

    def test_does_not_use_waitid(self):
        self.assertFalse(hasattr(procs.os, "waitid"))
        with procs.Child(["true"]) as child:
            self.assertTrue(child._wait_exit(block=True))
            self.assertIsNotNone(child.proc.returncode)  # reaped: the documented cost


@unittest.skipUnless(LINUX, "needs Linux")
class FakeKqueueTest(ChildChecks, unittest.TestCase):
    def patch_exit_check(self):
        self.select = fake_select()
        for p in (mock.patch.object(procs, "os", OsWithout("waitid")),
                  mock.patch.object(procs, "select", self.select)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        stats = self.select.stats
        self.assertEqual(stats["opened"], stats["closed"])  # no leaked kqueues

    def test_exit_seen_without_reaping(self):
        with procs.Child(sh("sleep 0.3")) as child:
            self.assertFalse(child._wait_exit(block=False))
            self.assertTrue(child._wait_exit(block=True))
            self.assertIsNone(child.proc.returncode)
            self.assertTrue(leader_exited(child.pid))  # still a zombie, pid reserved
            self.assertEqual(child.wait(), 0)
        self.assertGreaterEqual(self.select.stats["registered"], 1)

    def test_zombie_registration_counts_as_exited(self):
        with procs.Child(["true"]) as child:
            time.sleep(0.3)
            self.assertTrue(child._wait_exit(block=False))
            self.assertIsNone(child.proc.returncode)
            child.wait()
        self.assertGreaterEqual(self.select.stats["esrch"], 1)

    def test_blocking_wait_is_bounded_chunks(self):
        with mock.patch.object(procs, "KQUEUE_WAIT_SECONDS", 0.05):
            with procs.Child(["sleep", "0.4"]) as child:
                self.assertTrue(child._wait_exit(block=True))
                child.wait()
        self.assertGreater(self.select.stats["registered"], 2)

    def test_registration_error_falls_back_to_polling(self):
        real_kqueue = self.select.kqueue

        class Broken(real_kqueue):
            def control(self, changelist, max_events, timeout=None):
                if changelist:
                    raise PermissionError(errno.EPERM, "denied")
                return super().control(changelist, max_events, timeout)

        self.select.kqueue = Broken
        with procs.Child(sh("sleep 0.2; exit 4")) as child:
            with self.assertLogs("app.procs", "WARNING"):
                self.assertEqual(child.wait(), 4)
            self.assertTrue(child._kqueue_failed)


@unittest.skipUnless(LINUX, "needs Linux")
class CancelSuitePollFallback(test_cancel.CancelTest):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(procs, "os", OsWithout("waitid")),
                  mock.patch.object(procs, "select", types.SimpleNamespace())):
            p.start()
            self.addCleanup(p.stop)


@unittest.skipUnless(LINUX, "needs Linux")
class CancelSuiteFakeKqueue(test_cancel.CancelTest):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(procs, "os", OsWithout("waitid")),
                  mock.patch.object(procs, "select", fake_select())):
            p.start()
            self.addCleanup(p.stop)


if __name__ == "__main__":
    unittest.main()
