# -*- coding: utf-8 -*-
"""D-0801 / P3 — one pytest session runs batch after batch as WAVES.

TestHide hands an executor one batch at a time. Every batch is its own pytest process today, so a
class-scoped fixture is re-paid per batch — and on this farm that fixture is a Steam login and an
application start, seconds against test bodies measured in hundredths.

Measured on a 4-class x 5-test suite with a 2s class fixture and 4 nodeids per batch, pytest 9.1.1:

    per-batch, 5 processes (today)      18.1s    5 sessions, 5 modules, 8 class setups
    waves, one process                   9.3s    1 session,  1 module,  4 class setups
    the same 20 nodeids in one go        9.3s    <- the ceiling; a wave costs 0.4% over it

The tests below do not re-measure seconds — a timing assertion on a build agent is a flake waiting
to happen. They assert the thing the seconds came FROM: how many times each fixture was paid, and
in what order.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest


# A session fixture, a module fixture and one class fixture per class, each leaving a marker file
# per PAYMENT. One file per payment rather than a line in a shared file: two processes appending to
# one path lose writes on Windows, and that cost an afternoon in an earlier batch.
SUITE = """
    import os
    import pytest

    _N = [0]

    def _pay(name):
        _N[0] += 1
        open(os.path.join(str(os.path.dirname(__file__)),
                          "paid-%d-%s-%d.marker" % (os.getpid(), name, _N[0])), "a").close()

    @pytest.fixture(scope="session", autouse=True)
    def app():
        _pay("session")

    @pytest.fixture(scope="module", autouse=True)
    def mod():
        _pay("module")

    class TestA:
        @pytest.fixture(scope="class", autouse=True)
        def heavy(self):
            _pay("class-A")

        def test_1(self): pass
        def test_2(self): pass
        def test_3(self): pass

    class TestB:
        @pytest.fixture(scope="class", autouse=True)
        def heavy(self):
            _pay("class-B")

        def test_4(self): pass
        def test_5(self): pass
"""

A = ["test_suite.py::TestA::test_%d" % i for i in (1, 2, 3)]
B = ["test_suite.py::TestB::test_%d" % i for i in (4, 5)]


def _paid(pytester, name):
    return len(list(pytester.path.glob("paid-*-%s-*.marker" % name)))


# How long the feeder waits for a wave to report before giving up on it. Deliberately small: these
# waves are milliseconds of trivial tests, so any real completion is 1000x inside it, and the number
# is what a REGRESSION costs. A mutation run measured the first draft (120s) taking 25 minutes to
# report one broken engine — a suite that slow to say "broken" is a suite people stop running.
_WAVE_REPORT_DEADLINE = 20.0


def _feed(control, waves, delay=0.0, deadline=None):
    """Hand the live session one wave at a time, the way the client will: write the next wave only
    after the previous one has reported, then stop."""
    def worker():
        for n, wave in enumerate(waves):
            time.sleep(delay)
            tmp = control / ("wave-%d.tmp" % n)
            tmp.write_text(json.dumps({"nodeids": wave}), encoding="utf-8")
            os.replace(str(tmp), str(control / ("wave-%d.json" % n)))
            wait_until = time.time() + (deadline or _WAVE_REPORT_DEADLINE)
            while not (control / ("wave-%d.done.json" % n)).exists():
                if time.time() > wait_until:
                    break
                time.sleep(0.01)
        (control / "stop").write_text("", encoding="utf-8")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def run_waves(pytester, waves, *extra):
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, waves)
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), *extra, timeout=120)
    feeder.join(timeout=30)

    done = []
    for n in range(len(waves)):
        p = control / ("wave-%d.done.json" % n)
        done.append(json.loads(p.read_text(encoding="utf-8")) if p.exists() else None)
    return result, done


# --------------------------------------------------------------- the point of the whole change

def test_a_class_split_across_consecutive_waves_pays_its_fixture_once(pytester):
    """THE measurement. TestA's three tests arrive in two separate batches; the login happens once.

    Today each batch is a process, so this same split costs two logins — and the scheduler splits
    classes routinely, because it hands out fixed-size batches from a queue that knows nothing about
    class boundaries.
    """
    result, done = run_waves(pytester, [A[:2], A[2:] + B[:1], B[1:]])

    result.assert_outcomes(passed=5)
    assert _paid(pytester, "class-A") == 1, (
        "the class fixture was paid %d times across the split" % _paid(pytester, "class-A"))
    assert _paid(pytester, "session") == 1
    assert _paid(pytester, "module") == 1
    assert all(d is not None for d in done), "a wave never reported"


def test_fixture_payments_match_a_vanilla_run_of_the_same_nodeids(pytester):
    """Parity, not merely "fewer". A wave loop that skipped a fixture would also score well on the
    test above."""
    result, _ = run_waves(pytester, [A[:2], A[2:], B])
    waves = {name: _paid(pytester, name) for name in ("session", "module", "class-A", "class-B")}
    result.assert_outcomes(passed=5)

    for f in pytester.path.glob("paid-*.marker"):
        f.unlink()
    vanilla = pytester.runpytest_subprocess(*(A + B))
    vanilla.assert_outcomes(passed=5)
    plain = {name: _paid(pytester, name) for name in ("session", "module", "class-A", "class-B")}

    assert waves == plain, "waves=%r vanilla=%r" % (waves, plain)


def test_the_session_is_not_torn_down_between_waves(pytester):
    """The trap that makes a naive persistent session useless, pinned directly.

    Handing pytest nextitem=None at a wave boundary means "tear down everything, session included":
    measured, the application restarted once per wave and the gain was zero while every test still
    passed. One session payment for three waves is what says the sentinel is doing its job.
    """
    result, _ = run_waves(pytester, [A[:1], A[1:2], A[2:]])

    result.assert_outcomes(passed=3)
    assert _paid(pytester, "session") == 1, (
        "the session was rebuilt %d times — nextitem at the wave boundary is wrong"
        % _paid(pytester, "session"))


# --------------------------------------------------------------- reporting per wave

def test_every_wave_reports_a_verdict_for_every_nodeid_it_was_given(pytester):
    """The scheduler cannot wait for the session to end: a nodeid it assigned and never hears about
    stays `running` until a sweep reclaims it, and then goes to another executor."""
    _, done = run_waves(pytester, [A[:2], A[2:] + B[:1]])

    assert [len(d["results"]) for d in done] == [2, 2]
    assert [r["nodeid"] for r in done[0]["results"]] == A[:2]
    assert all(r["outcome"] == "passed" for d in done for r in d["results"])
    assert all("duration" in r for d in done for r in d["results"])


def test_a_nodeid_this_session_cannot_see_is_reported_not_dropped(pytester):
    """An executor started with a narrower scope than the batches it is given. Silence here is the
    worst outcome available: the nodeid is neither run, nor failed, nor re-queued."""
    _, done = run_waves(pytester, [A[:1] + ["test_suite.py::TestZ::test_gone"]])

    assert done[0]["not_collected"] == ["test_suite.py::TestZ::test_gone"]
    assert [r["nodeid"] for r in done[0]["results"]] == A[:1]


def test_a_failure_is_reported_as_failed_and_an_error_as_error(pytester):
    """The backend treats them differently — an error is a broken environment, a failure is a broken
    test — so a wave verdict that collapses them sends the wrong node to triage."""
    pytester.makepyfile(test_suite="""
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("environment")

        def test_ok(): pass
        def test_fails(): assert False
        def test_errors(broken): pass
        def test_skipped(): pytest.skip("nope")
    """)
    control = pytester.path / "control"
    control.mkdir()
    ids = ["test_suite.py::test_ok", "test_suite.py::test_fails",
           "test_suite.py::test_errors", "test_suite.py::test_skipped"]
    feeder = _feed(control, [ids])
    pytester.runpytest_subprocess("test_suite.py", "--testhide-session-dir", str(control),
                                  timeout=120)
    feeder.join(timeout=30)

    results = {r["nodeid"]: r["outcome"]
               for r in json.loads((control / "wave-0.done.json").read_text(encoding="utf-8"))["results"]}
    assert results == {
        "test_suite.py::test_ok": "passed",
        "test_suite.py::test_fails": "failed",
        "test_suite.py::test_errors": "error",
        "test_suite.py::test_skipped": "skipped",
    }


# --------------------------------------------------------------- failure at a wave boundary

def test_a_teardown_error_on_a_wave_boundary_is_reported_and_does_not_kill_the_session(pytester):
    """Measured before the fix, with the boundary falling between two classes:

        vanilla                     rc=1   4 passed, 1 error
        waves, unguarded boundary   rc=3   2 passed          <- INTERNALERROR

    The exception escaped pytest_runtestloop, so the executor died and the NEXT wave's nodeids never
    ran and never reported. On this farm a failing class teardown is a logout that did not happen —
    the first sign the machine is wedged — and the scheduler kept feeding that node work.
    """
    pytester.makepyfile(test_suite="""
        import pytest

        class TestA:
            @pytest.fixture(scope="class", autouse=True)
            def heavy(self):
                yield
                raise RuntimeError("logout failed")

            def test_1(self): pass
            def test_2(self): pass

        class TestB:
            def test_3(self): pass
            def test_4(self): pass
    """)
    control = pytester.path / "control"
    control.mkdir()
    a = ["test_suite.py::TestA::test_1", "test_suite.py::TestA::test_2"]
    b = ["test_suite.py::TestB::test_3", "test_suite.py::TestB::test_4"]

    feeder = _feed(control, [a, b])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    result.assert_outcomes(passed=4, errors=1)
    result.stdout.fnmatch_lines(["*logout failed*"])
    assert (control / "wave-1.done.json").exists(), "the second wave never ran"


def test_maxfail_left_in_the_script_would_lose_every_later_wave(pytester):
    """Why the batch guard is a PRECONDITION for a persistent session rather than a nicety.

    runtestprotocol sets nextitem=None when session.shouldfail, and the loop then stops. Per batch
    that costs the rest of ONE batch; in a persistent session it costs the rest of the EXECUTOR.
    Measured with the guard disabled: 0 of 2 waves completed and wave 1 was never delivered.

    Here the guard IS applied (a session dir implies --testhide-batch), so -x is neutralised and
    both waves run.
    """
    pytester.makepyfile(test_suite="""
        def test_1(): pass
        def test_2(): assert False, "planted"
        def test_3(): pass
        def test_4(): pass
    """)
    control = pytester.path / "control"
    control.mkdir()
    ids = ["test_suite.py::test_%d" % i for i in (1, 2, 3, 4)]

    feeder = _feed(control, [ids[:2], ids[2:]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), "-x", timeout=120)
    feeder.join(timeout=30)

    result.assert_outcomes(passed=3, failed=1)
    assert (control / "wave-1.done.json").exists(), (
        "-x survived into the session: the second wave never ran")


# --------------------------------------------------------------- the off switch

def test_an_ordinary_run_is_untouched(pytester):
    """Non-vacuity, and the safety property: with no control directory the loop stands aside and
    pytest runs exactly as it always did."""
    pytester.makepyfile(test_suite=SUITE)

    result = pytester.runpytest_subprocess(*(A + B))

    result.assert_outcomes(passed=5)
    assert _paid(pytester, "session") == 1
    assert not any("[testhide] wave" in ln for ln in result.outlines)


def test_the_control_directory_can_arrive_through_the_environment(pytester, monkeypatch):
    """The delivery that matters in production. A job that starts its tests with its own command
    template has nowhere for the client to insert an argument, and environment variables are
    already threaded into that path — so the env var is the primary source, not a convenience."""
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()
    monkeypatch.setenv("TESTHIDE_SESSION_DIR", str(control))

    feeder = _feed(control, [A[:2], A[2:]])
    result = pytester.runpytest_subprocess("test_suite.py", timeout=180)
    feeder.join(timeout=30)

    result.assert_outcomes(passed=3)
    assert _paid(pytester, "class-A") == 1
    assert (control / "wave-1.done.json").exists()


def test_stop_with_no_waves_ends_the_session_cleanly(pytester):
    """A client that dies between spawning the executor and assigning it anything must not leave a
    pytest process holding the node."""
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()
    (control / "stop").write_text("", encoding="utf-8")

    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)

    result.assert_outcomes()
    assert _paid(pytester, "session") == 0, "a session with no waves paid for the application"


# --------------------------------------------------------------- one broken file, one executor

def test_a_broken_module_does_not_take_the_whole_executor_down(pytester):
    """A collection error ends a one-shot run. It must NOT end a persistent one.

    An executor holds assignments: aborting at collection means every nodeid this node was given,
    in this wave and every wave after it, comes back with no result — not run, not failed, not
    re-queued. The sweep then hands them to another executor, which imports the same broken module
    and dies the same way. One un-importable file takes the fleet down a node at a time, silently.

    Serving the waves anyway means the healthy tests run and report, and the ones inside the broken
    file come back named in `not_collected`.
    """
    pytester.makepyfile(test_broken="import totally_missing_module_xyz\n\ndef test_x(): pass\n")
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [A[:2], ["test_broken.py::test_x"] + A[2:]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "test_broken.py", "--testhide-session-dir", str(control),
        "--continue-on-collection-errors", timeout=120)
    feeder.join(timeout=30)

    # Both waves ran and reported, and the healthy nodeids have verdicts.
    done0 = json.loads((control / "wave-0.done.json").read_text(encoding="utf-8"))
    done1 = json.loads((control / "wave-1.done.json").read_text(encoding="utf-8"))
    assert [r["nodeid"] for r in done0["results"]] == A[:2]
    assert [r["nodeid"] for r in done1["results"]] == A[2:]
    assert done1["not_collected"] == ["test_broken.py::test_x"]
    assert _paid(pytester, "class-A") == 1, "the split class still paid its fixture once"


def test_a_broken_module_serves_waves_even_without_continue_on_collection_errors(pytester):
    """The flag above is the customer's choice, and the farm's scripts do not carry it. Without it,
    pytest's own loop aborts the session — which is exactly the behaviour a persistent session must
    not inherit."""
    pytester.makepyfile(test_broken="import totally_missing_module_xyz\n")
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [A[:2], A[2:]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "test_broken.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    assert (control / "wave-0.done.json").exists(), "the executor died at collection"
    assert (control / "wave-1.done.json").exists()
    result.stdout.fnmatch_lines(["*collection error*serving waves anyway*"])


def test_a_wave_with_nothing_collectable_ends_the_session(pytester):
    """A wave where NOT ONE nodeid is visible is not a broken test file — a broken file leaves its
    siblings collectable. It is a session whose scope does not cover what it is being assigned: an
    executor started on one file, or in a different rootdir, than the queue was built from.

    Every later wave would return the same empty answer, so serving them means holding a node while
    reporting nothing, indefinitely. Ending lets the sweep give the work to a healthy executor.
    """
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    # 3s, not the default 20: wave 1 is expected NEVER to report, and paying the full
    # regression deadline for an outcome the test WANTS would put 20 idle seconds in
    # every run of this suite.
    feeder = _feed(control, [["nowhere.py::test_a", "nowhere.py::test_b"], A[:2]],
                   deadline=3.0)
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done0 = json.loads((control / "wave-0.done.json").read_text(encoding="utf-8"))
    assert done0["results"] == []
    assert len(done0["not_collected"]) == 2
    assert not (control / "wave-1.done.json").exists(), (
        "the session kept serving waves it cannot answer")
    result.stdout.fnmatch_lines(["*no collectable tests at all*"])


def test_a_partial_miss_is_not_fatal(pytester):
    """The other half of the same rule, and the reason it is not simply "any miss ends it": one
    renamed or deleted test must not stop an executor that is running everything else correctly."""
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [A[:1] + ["gone.py::test_x"], A[1:]])
    pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    assert (control / "wave-1.done.json").exists(), "a single missing nodeid ended the session"
    done1 = json.loads((control / "wave-1.done.json").read_text(encoding="utf-8"))
    assert [r["nodeid"] for r in done1["results"]] == A[1:]
    assert _paid(pytester, "class-A") == 1


# --------------------------------------------------------------- the report still gets published

def test_the_junit_report_carries_every_wave(pytester):
    """The junit is still assembled once, at session finish — waves do not change that. What they
    DO change is that the session must be recognised as one that ran tests: the guard that stops a
    --collect-only run from replacing a real report with tests="0" is the same guard that would
    discard this one.

    Two implementations of pytest_runtestloop are involved and both are tryfirst on a firstresult
    hook, so before this test the published report depended on the order pluggy happens to call
    them in. Every wave passing and the report vanishing is not a failure anyone would look for.
    """
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [A[:2], A[2:] + B])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control),
        "--report-xml", "junittests.xml", timeout=120)
    feeder.join(timeout=30)

    result.assert_outcomes(passed=5)
    report = pytester.path / "junittests.xml"
    assert report.exists(), "the report was discarded"

    import xml.etree.ElementTree as ET
    cases = {c.get("name") for c in ET.parse(str(report)).getroot().iter("testcase")}
    assert len(cases) == 5, "the report has %d cases, not 5: %r" % (len(cases), cases)


def test_the_wave_loop_marks_the_session_itself(pytester):
    """Driven directly, because the end-to-end test above cannot distinguish it.

    Two implementations of pytest_runtestloop are tryfirst on a firstresult hook. Today pluggy calls
    the instance one first, so it sets the flag and the report survives — which means deleting the
    line below leaves the whole suite green (measured). That is an ordering the tests cannot see,
    and the cost of it changing is the entire report being discarded on a run where everything
    passed. So the line is asserted where it can be: on the function itself.
    """
    from testhide_plugin import plugin as tp

    class _Reporter:
        _runtestloop_entered = False

    class _PM:
        def __init__(self, reporter):
            self._r = reporter

        def get_plugin(self, name):
            return self._r if name == "testhide_plugin_active" else None

        def register(self, *a, **k):
            return None

        def unregister(self, *a, **k):
            return None

    control = pytester.path / "ctl"
    control.mkdir()
    (control / "stop").write_text("", encoding="utf-8")      # end immediately

    reporter = _Reporter()

    class _Opt:
        testhide_session_dir = str(control)
        collectonly = False
        continue_on_collection_errors = False

    class _Cfg:
        option = _Opt()
        pluginmanager = _PM(reporter)

    class _SetupState:
        def teardown_exact(self, nextitem):
            return None

    class _Session:
        config = _Cfg()
        testsfailed = 0
        items = []
        shouldfail = False
        shouldstop = False
        _setupstate = _SetupState()

    assert tp.pytest_runtestloop(_Session()) is True
    assert reporter._runtestloop_entered is True, (
        "the wave loop left the session marked as one that never ran tests — "
        "pytest_sessionfinish would discard the report")
