"""Load-tolerant test deadlines; no production clocks are replaced."""

from contextlib import ExitStack
import math
import os
import signal
import subprocess
import time
from unittest import mock


TIME_SCALE = float(os.environ.get("ORCEXE_TEST_TIME_SCALE", "1"))
if not math.isfinite(TIME_SCALE) or TIME_SCALE <= 0:
    raise ValueError("ORCEXE_TEST_TIME_SCALE must be finite and > 0")


def scaled(seconds):
    return seconds * TIME_SCALE


def wait_for(predicate, seconds=15):
    """Wait for state, with a distinct failure from the property under test."""
    deadline = time.monotonic() + scaled(seconds)
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("child never became ready")
        time.sleep(.01)


def run_when_ready(runner, argv, ready, add_cleanup, start=None, **options):
    """Start the runner's real deadline only after a child setup handshake.

    Inject only Popen's already-running result, leaving clocks, signals, pipe
    readers and timeout/grace decisions real. Windows attaches the child to the
    runner's real Job before resuming it, so setup descendants are also owned.
    A start callback or on_line handshake can release a prepared leader.
    """
    from orcexe import proc

    job = proc._WindowsJob() if os.name == "nt" else None
    spawn = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 4}
             if os.name == "nt" else {"start_new_session": True})
    launch = dict(cwd=options["cwd"], env=options["env"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, **spawn)
    process = subprocess.Popen(argv, **launch)

    def cleanup():
        # Also own cleanup when a deliberately broken runner leaves a tree.
        if job is not None:
            try:
                if job.alive():
                    job.stop(process, options["env"], 0)
            finally:
                job.close()
        else:
            process.poll()  # Reap a zombie leader before signalling its group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS can report EPERM if the leader exited after poll().
                if process.poll() is None:
                    raise
        process.wait(timeout=scaled(5))
        process.stdout.close()
        process.stderr.close()

    # Defer cleanup until after assertions, so it cannot hide a missed tree kill.
    add_cleanup(cleanup)
    if job is not None:
        job.attach_and_resume(process)
    wait_for(ready)

    def supplied_process(*args, **kwargs):
        if args != (argv,) or kwargs != launch:
            raise AssertionError("prepared child differs from runner's launch request")
        if start is not None:
            start()
        return process

    with ExitStack() as stack:
        stack.enter_context(mock.patch("orcexe.proc.subprocess.Popen",
                                       side_effect=supplied_process))
        if job is not None:
            stack.enter_context(mock.patch.object(proc, "_WindowsJob", return_value=job))
            stack.enter_context(mock.patch.object(job, "attach_and_resume"))
            # Leave the handle available for assertion-independent cleanup.
            stack.enter_context(mock.patch.object(job, "close"))
        return runner(argv, **options)
