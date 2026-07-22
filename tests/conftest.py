"""Session-wide clean-shutdown assertion (P1-1, Sol10 rc4 Session 8).

The audit observed a run where every assertion passed (197 passed) but the
pytest *process* stayed alive until externally killed -- a passing summary
is not proof the interpreter can actually exit. This checks the concrete
suspects named in the report (multiprocessing children, non-daemon
threads, surviving `cli.py` subprocesses) once, at session end, and forces
a non-zero exit if any of them are still around -- release.sh's `timeout
900s ... pytest` wrapper only catches the case where nothing bounds the
hang at all; this catches the case where pytest itself thinks it's done.
"""

import multiprocessing
import os
import sys
import threading

_main_thread_idents_at_start = None


def pytest_sessionstart(session):
    global _main_thread_idents_at_start
    _main_thread_idents_at_start = {t.ident for t in threading.enumerate()}


def _surviving_cli_subprocesses():
    # Linux only (the release matrix and every VPS pipeline run Linux) --
    # skip silently elsewhere rather than false-failing on an unsupported
    # host's /proc layout.
    if not sys.platform.startswith("linux"):
        return []
    hits = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        if int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "cli.py" in cmdline:
            hits.append(f"pid={pid} cmdline={cmdline!r}")
    return hits


def pytest_sessionfinish(session, exitstatus):
    problems = []

    leaked_children = multiprocessing.active_children()
    if leaked_children:
        problems.append(
            "multiprocessing.active_children() is not empty: "
            + ", ".join(f"{c.name}(pid={c.pid})" for c in leaked_children)
        )
        for child in leaked_children:
            child.terminate()
            child.join(timeout=5)

    unexpected_threads = [
        t
        for t in threading.enumerate()
        if t is not threading.main_thread()
        and not t.daemon
        and t.ident not in (_main_thread_idents_at_start or set())
    ]
    if unexpected_threads:
        problems.append(
            "unexpected non-daemon thread(s) still alive: "
            + ", ".join(f"{t.name}(ident={t.ident})" for t in unexpected_threads)
        )

    cli_survivors = _surviving_cli_subprocesses()
    if cli_survivors:
        problems.append("surviving cli.py subprocess(es): " + "; ".join(cli_survivors))

    if problems:
        print(
            "\n".join(["ASSAYER CLEAN-SHUTDOWN CHECK FAILED:"] + [f"  - {p}" for p in problems]),
            file=sys.stderr,
        )
        session.exitstatus = 1
