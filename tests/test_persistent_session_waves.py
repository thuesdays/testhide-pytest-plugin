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
    result, done = run_waves(pytester, [A[:2], A[2:], B])
    waves = {name: _paid(pytester, name) for name in ("session", "module", "class-A", "class-B")}
    result.assert_outcomes(passed=5)
    # Not incidental. Both sides of the comparison below equal {session:1, module:1, class-A:1,
    # class-B:1} for ANY arrangement that runs these five nodeids without a teardown in between --
    # including pytest's own loop with the wave engine switched off entirely. Measured: forcing the
    # engine off left this test green and only its neighbour red, and the single line that killed
    # that mutant was this one, which this test used to throw away.
    assert all(d is not None for d in done), "a wave never reported"

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


def test_a_wave_naming_a_file_this_session_never_collected_ends_it_after_two(pytester):
    """A wave where not one nodeid is visible AND the FILE is unknown is a session whose scope does
    not cover what it is being assigned: an executor started on one file, or in a different rootdir,
    than the queue was built from.

    Every later wave returns the same empty answer, so serving them means holding a node while
    reporting nothing, indefinitely. Ending lets the sweep give the work to a healthy executor.

    Two waves, not one — see the test below for the half that costs more than it saves.
    """
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    # 3s, not the default 20: wave 2 is expected NEVER to report, and paying the full
    # regression deadline for an outcome the test WANTS would put 20 idle seconds in
    # every run of this suite.
    feeder = _feed(control, [["nowhere.py::test_a"], ["nowhere.py::test_b"], A[:2]],
                   deadline=3.0)
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done0 = json.loads((control / "wave-0.done.json").read_text(encoding="utf-8"))
    assert done0["results"] == []
    assert done0["not_collected"] == ["nowhere.py::test_a"]
    assert (control / "wave-1.done.json").exists()
    assert not (control / "wave-2.done.json").exists(), (
        "the session kept serving waves it cannot answer")
    result.stdout.fnmatch_lines(["*does not know*", "*waves in a row named files*"])


def test_a_single_renamed_nodeid_does_not_end_a_one_test_per_wave_session(pytester):
    """The rule above, expressed in WAVES, degenerated into "any total miss is fatal" — because a
    wave is `max_tests_per_one_execute` tests and every writer of that setting defaults it to 1.

    So one renamed or deleted test, the ordinary case the partial-miss branch exists for, ended the
    whole persistent session, and every batch that executor still held went unanswered. Measured
    before the fix on exactly these four waves: waves 2 and 3 had no done file at all and the run
    reported "1 passed". The partial-miss test next to this one uses two-nodeid waves and therefore
    could not see it.
    """
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [[A[0]], ["test_suite.py::TestA::test_renamed"], [A[1]], [A[2]]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    for n in (2, 3):
        assert (control / ("wave-%d.done.json" % n)).exists(), (
            "one renamed test ended the session; wave %d was never served" % n)
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(["*the file is collected but the test is not*"])


def test_a_wave_entirely_inside_a_broken_module_does_not_end_the_session(pytester):
    """The rule's premise — "a broken file leaves its siblings collectable, so a TOTAL miss must be
    a scope mismatch" — is false whenever a batch lands inside the broken file. Batches are
    contiguous slices of a discovery-ordered queue, so that is not a corner case; at the default
    batch of one it happens on the FIRST nodeid of the broken file.

    Worse than ending: the diagnosis printed was about rootdir and the shape of the
    --testhide-session-dir token, which is the wrong place to send whoever reads it. The collection
    error is right there in the same log.
    """
    pytester.makepyfile(test_suite=SUITE)
    pytester.makepyfile(test_broken="import totally_missing_module_xyz\n\n"
                                    "def test_x(): pass\ndef test_y(): pass\n")
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [A[:2],
                             ["test_broken.py::test_x", "test_broken.py::test_y"],
                             A[2:]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "test_broken.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    assert (control / "wave-2.done.json").exists(), (
        "a batch inside a broken module took the whole executor down")
    done2 = json.loads((control / "wave-2.done.json").read_text(encoding="utf-8"))
    assert [r["nodeid"] for r in done2["results"]] == A[2:]
    # A broken module is the OTHER shape of a wave with nothing collectable, and it reaches the same
    # baseline as a renamed nodeid does — but one branch later, so a change that re-wipes on this
    # one alone would leave the renamed-nodeid test green. What that costs is here: wave 1 collected
    # nothing, so test_2 is still held with its class teardown unrun, and a wiped baseline makes
    # wave 2 amend an already-published `passed` to `missing`, which the client writes as <error>.
    assert done2["amended"] == [], (
        "a wave inside a broken module wiped the baseline and the previous wave's test was "
        "re-published: %r" % done2["amended"])
    result.stdout.fnmatch_lines(["*COLLECTION FAILED*test_broken.py*"])
    assert "nodeids are relative to rootdir" not in result.stdout.str(), (
        "the operator was sent to look at rootdir for what is an import error")


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
    # First: that these five tests ran as WAVES at all. Every assertion below is equally true of an
    # ordinary run of the same file with --report-xml -- verified: 5 passed, report written, five
    # cases -- so without this line the test is named after a path it never visits.
    assert (control / "wave-1.done.json").exists(), "the waves never ran"
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


def test_the_nothing_collectable_message_names_the_rootdir_cause(pytester):
    """The most likely cause of this symptom is not a mistyped nodeid — it is a rootdir mismatch,
    and it is invisible unless the message says so.

    Measured on pytest 9.1.1, same suite, same working directory:

        (no flag)                                        test_suite.py::test_1
        --testhide-session-dir inside_the_suite          test_suite.py::test_1
        --testhide-session-dir ../outside   TWO TOKENS   suite/test_suite.py::test_1   <-- renamed
        --testhide-session-dir=../outside   ONE TOKEN    test_suite.py::test_1
        TESTHIDE_SESSION_DIR=../outside                  test_suite.py::test_1

    pytest resolves rootdir BEFORE it knows a plugin's options, so a value passed as its own token
    is counted as a path argument and rootdir moves up to the common ancestor of the tests and that
    path. Nodeids are relative to rootdir, so every one of them is renamed and every wave is
    entirely uncollectable — with a temp directory for a control channel, which is exactly what the
    client would pass, that is the whole feature dead. The environment variable and the `=` form are
    both immune; the env var is the production channel for other reasons already.
    """
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [["wrong/prefix/test_suite.py::TestA::test_1"],
                             ["wrong/prefix/test_suite.py::TestA::test_2"]], deadline=3.0)
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    result.stdout.fnmatch_lines([
        "*waves in a row named files this session never collected*",
        "*rootdir:*",
        "*this session collected ids like:*",
        "*the wave asked for:*",
        "*nodeids are relative to rootdir*",
    ])


# --------------------------------------------------------------- the poisoned session

POISONED = """
    import pytest

    @pytest.fixture(scope="session", autouse=True)
    def app():
        raise RuntimeError("steam login failed")

""" + "".join("    def test_%d(): pass\n" % i for i in range(1, 31))


def test_a_session_whose_fixture_died_stops_asking_for_work(pytester):
    """pytest caches a fixture's exception and never retries it, so a session whose session-scoped
    fixture died once errors every test it is ever given — instantly. Measured:

        wave 0   ['error', 'error']   0.245s   <- the fixture actually ran
        wave 1   ['error', 'error']   0.038s   <- cached; nothing ran at all

    38 milliseconds per wave, and then it asks for more. Against a FIFO queue this executor
    out-competes every healthy node and turns the whole suite into errors on one bad machine: the
    tests are fine, the report is not, and the fastest worker on the farm is the broken one.
    """
    pytester.makepyfile(test_suite=POISONED)
    control = pytester.path / "control"
    control.mkdir()
    ids = ["test_suite.py::test_%d" % i for i in range(1, 31)]

    feeder = _feed(control, [ids[0:10], ids[10:20], ids[20:30]], deadline=3.0)
    pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    assert (control / "wave-0.done.json").exists()
    assert (control / "wave-1.done.json").exists()
    assert not (control / "wave-2.done.json").exists(), (
        "the poisoned session kept taking work")

    done1 = json.loads((control / "wave-1.done.json").read_text(encoding="utf-8"))
    assert all(r["outcome"] == "error" for r in done1["results"])


def test_two_adjacent_single_test_error_waves_do_not_end_a_healthy_session(pytester):
    """The detector counted WAVES and reasoned about batches ("a batch can land entirely inside one
    broken class"), but the wave size it was calibrated against is a job setting the plugin cannot
    see, and every writer of it defaults to 1. So the threshold in practice was "two erroring tests
    in a row" — which is one ordinary broken class fixture shared by two neighbouring tests, not a
    session that will never recover.

    Measured before the fix on exactly these four waves: the session ended after wave 1 and the two
    healthy tests were never served. The two existing tests could not see it: one uses two-nodeid
    waves, the other alternates bad/ok specifically so the reset fires.
    """
    pytester.makepyfile(test_suite="""
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("one shared class fixture")

        def test_bad_1(broken): pass
        def test_bad_2(broken): pass
        def test_ok_3(): pass
        def test_ok_4(): pass
    """)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [["test_suite.py::test_bad_1"], ["test_suite.py::test_bad_2"],
                             ["test_suite.py::test_ok_3"], ["test_suite.py::test_ok_4"]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    for n in (2, 3):
        assert (control / ("wave-%d.done.json" % n)).exists(), (
            "two erroring tests ended a session with healthy work left; wave %d unserved" % n)
    result.assert_outcomes(passed=2, errors=2)


def test_one_all_error_wave_is_not_enough_to_give_up(pytester):
    """A batch can legitimately land entirely inside one broken class, and the executor is fine.
    Giving up on the first one would turn a local failure into a lost node."""
    pytester.makepyfile(test_suite="""
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("just this one")

        def test_a(broken): pass
        def test_b(broken): pass
        def test_c(): pass
        def test_d(): pass
    """)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [["test_suite.py::test_a", "test_suite.py::test_b"],
                             ["test_suite.py::test_c", "test_suite.py::test_d"]])
    pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    assert (control / "wave-1.done.json").exists(), "one bad batch ended a healthy session"
    done1 = json.loads((control / "wave-1.done.json").read_text(encoding="utf-8"))
    assert all(r["outcome"] == "passed" for r in done1["results"])


def test_all_error_waves_have_to_be_CONSECUTIVE(pytester):
    """Found by mutation: without the reset, two all-error waves anywhere in a session end it.

    A long suite can easily have one broken class early and another later, with healthy batches in
    between. Treating those as a poisoned session takes a node that is doing its job and hands its
    remaining work back to the queue — the false positive costs more than the detector saves.
    """
    pytester.makepyfile(test_suite="""
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("local")

        def test_bad_1(broken): pass
        def test_ok_1(): pass
        def test_bad_2(broken): pass
        def test_ok_2(): pass
    """)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [["test_suite.py::test_bad_1"],
                             ["test_suite.py::test_ok_1"],
                             ["test_suite.py::test_bad_2"],
                             ["test_suite.py::test_ok_2"]])
    pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    assert (control / "wave-3.done.json").exists(), (
        "non-consecutive all-error waves ended a session that was still working")
    done3 = json.loads((control / "wave-3.done.json").read_text(encoding="utf-8"))
    assert done3["results"][0]["outcome"] == "passed"


# ---------------------------------------------------------------- a failure must never arrive
#                                                                   as a pass

LATE_TEARDOWN = """
    import pytest

    class TestA:
        @pytest.fixture(scope="class", autouse=True)
        def heavy(self):
            yield
            raise RuntimeError("steam logout failed")

        def test_1(self): pass
        def test_2(self): pass

    class TestB:
        def test_3(self): pass
"""


def _rows(payload, key):
    return {r["nodeid"]: r["outcome"] for r in (payload.get(key) or [])}


def test_a_class_teardown_error_after_a_waves_last_test_is_published_as_an_amendment(pytester):
    """The one direction that must never happen: a failure arriving as a pass.

    The last item of a wave is run with a sentinel for `nextitem` that deliberately keeps its class,
    module and session alive across the boundary — that IS the feature. So its class teardown runs
    one wave later, after the wave that owned it has already published `passed` and frozen it in a
    done file the client has read.

    Before this, the corrected verdict was logged into a `_wave_reports` dict that the very next
    line cleared, so it reached nothing: the terminal said "3 passed, 1 error", every done file said
    passed, the synthesised junit carried no <error>, and the build was green. At the default batch
    of one this is the LAST test of every wave, which is every test.
    """
    pytester.makepyfile(test_suite=LATE_TEARDOWN)
    control = pytester.path / "control"
    control.mkdir()

    ids = ["test_suite.py::TestA::test_1", "test_suite.py::TestA::test_2",
           "test_suite.py::TestB::test_3"]
    feeder = _feed(control, [[ids[0]], [ids[1]], [ids[2]]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done = [json.loads((control / ("wave-%d.done.json" % n)).read_text(encoding="utf-8"))
            for n in range(3)]

    # The class ends when wave 2 asks for TestB, so that is where the correction appears.
    assert _rows(done[2], "amended") == {ids[1]: "error"}, (
        "the class teardown failure never reached the wave protocol: %r" % done[2])
    # ...and nowhere else, because nothing else changed.
    assert done[0]["amended"] == [] and done[1]["amended"] == []
    # The published verdict for test_2 was passed. Both statements are true at once; that is
    # precisely why the amendment channel has to exist rather than the row being rewritten.
    assert _rows(done[1], "results") == {ids[1]: "passed"}
    result.assert_outcomes(passed=3, errors=1)


def test_a_teardown_error_on_the_FINAL_wave_is_not_swallowed(pytester):
    """The last item of the LAST wave has the same deferred teardown and no boundary left to run in.

    It used to unwind under a bare `except Exception: print(...)`, so the exception became a line of
    stdout glued into the middle of the progress bar and nothing else: no report, no
    session.testsfailed, no exit code. Measured against vanilla on this file — vanilla rc=1
    "2 passed, 1 error", waves rc=0 "2 passed".
    """
    pytester.makepyfile(test_suite=LATE_TEARDOWN)
    control = pytester.path / "control"
    control.mkdir()

    ids = ["test_suite.py::TestA::test_1", "test_suite.py::TestA::test_2"]
    feeder = _feed(control, [[ids[0]], [ids[1]]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    ended = control / "session-ended.json"
    assert ended.exists(), "the session published no end marker at all"
    assert _rows(json.loads(ended.read_text(encoding="utf-8")), "amended") == {ids[1]: "error"}
    result.assert_outcomes(passed=2, errors=1)
    assert result.ret == 1, "a session that ended on an error reported success"


def test_the_session_end_marker_is_written_even_when_nothing_failed(pytester):
    """It is the marker, not the error channel: a client has to be able to tell "the session
    finished" from "the process died", and an end file that only appears on failure cannot."""
    _, done = run_waves(pytester, [A[:2], A[2:]])
    control = pytester.path / "control"

    ended = control / "session-ended.json"
    assert ended.exists()
    assert json.loads(ended.read_text(encoding="utf-8"))["amended"] == []
    assert all(d is not None for d in done)


def test_a_wave_with_nothing_collectable_does_not_amend_the_previous_wave_to_missing(pytester):
    """The amendment channel must only ever carry a verdict that CHANGED.

    A wave whose every nodeid is uncollectable — one renamed test, at the default batch of one — is
    served and reported empty, and correctly does not settle the held item's deferred teardown,
    because there is no next item to settle it against. The item stays held. Wiping the wave
    baseline anyway threw away the phase reports and the published outcome of a test that had
    already reported `passed`, so the next real wave amended it to `missing` — which the client
    writes as <error> and the backend reads as failed.

    Measured on the shipped plugin with waves [test_1], [renamed], [test_2]: wave 2 carried
    {'nodeid': '...::test_1', 'outcome': 'missing'} while pytest itself said "2 passed".
    """
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [[A[0]], ["test_suite.py::TestA::test_renamed"], [A[1]]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done = [json.loads((control / ("wave-%d.done.json" % n)).read_text(encoding="utf-8"))
            for n in range(3)]

    assert done[1]["not_collected"] == ["test_suite.py::TestA::test_renamed"], (
        "the wave under test was not the empty one this test needs: %r" % done[1])
    assert done[2]["amended"] == [], (
        "a wave that collected nothing wiped the baseline and the previous wave's test was "
        "re-published: %r" % done[2]["amended"])
    assert _rows(done[0], "results") == {A[0]: "passed"}
    assert _rows(done[2], "results") == {A[1]: "passed"}
    result.assert_outcomes(passed=2)


def test_a_final_wave_with_nothing_collectable_leaves_the_end_marker_clean(pytester):
    """The same wipe, one line later. When the LAST wave is the uncollectable one, the poisoned
    baseline reaches `session-ended.json` instead of a done file — and the client synthesises a
    junit out of that marker, so the invented `missing` row is what gets sent."""
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [[A[0]], ["test_suite.py::TestA::test_renamed"]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done1 = json.loads((control / "wave-1.done.json").read_text(encoding="utf-8"))
    assert done1["results"] == [] and done1["not_collected"], (
        "the last wave was not the empty one this test needs: %r" % done1)
    ended = json.loads((control / "session-ended.json").read_text(encoding="utf-8"))
    assert ended["amended"] == [], (
        "the end marker invented a verdict for a test that had already passed: %r" % ended)
    result.assert_outcomes(passed=1)


def test_a_real_correction_still_survives_an_uncollectable_wave_in_between(pytester):
    """The other direction, so the fix above cannot be "stop amending". A class teardown that fails
    at a boundary two waves later still has to be published: preserving the baseline is what makes
    the correction expressible as a CHANGE rather than as a verdict conjured from nothing."""
    pytester.makepyfile(test_suite=LATE_TEARDOWN)
    control = pytester.path / "control"
    control.mkdir()

    ids = ["test_suite.py::TestA::test_1", "test_suite.py::TestA::test_2",
           "test_suite.py::TestB::test_3"]
    feeder = _feed(control, [[ids[0]], [ids[1]], ["test_suite.py::TestA::test_renamed"], [ids[2]]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done = [json.loads((control / ("wave-%d.done.json" % n)).read_text(encoding="utf-8"))
            for n in range(4)]

    assert done[2]["results"] == [] and done[2]["not_collected"], (
        "wave 2 was not the empty one this test needs: %r" % done[2])
    # TestA's class ends when wave 3 asks for TestB, so that is where the correction appears --
    # unchanged by the empty wave sitting between the test and its own teardown.
    assert _rows(done[3], "amended") == {ids[1]: "error"}, (
        "the deferred teardown failure was lost across the empty wave: %r" % done[3])
    assert done[0]["amended"] == [] and done[1]["amended"] == []
    result.assert_outcomes(passed=3, errors=1)


def test_a_function_scoped_teardown_error_lands_in_ITS_OWN_wave(pytester):
    """Only class, module and session teardown genuinely has to wait for the next wave. The item's
    own function-scoped teardown does not, and vanilla runs it between two tests of the same class.

    The boundary sentinel used to hold the finished item's FULL chain, which pops nothing — so this
    ordinary case was deferred too, and a per-test cleanup that fails was published as a pass by the
    wave that ran it. Trimming the sentinel's chain to everything ABOVE the item restores parity
    with vanilla and leaves only the genuinely-undecidable scopes to the amendment channel.
    """
    pytester.makepyfile(test_suite="""
        import pytest

        @pytest.fixture
        def fn():
            yield
            raise RuntimeError("per-test cleanup failed")

        def test_1(fn): pass
        def test_2(): pass
    """)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [["test_suite.py::test_1"], ["test_suite.py::test_2"]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done0 = json.loads((control / "wave-0.done.json").read_text(encoding="utf-8"))
    assert _rows(done0, "results") == {"test_suite.py::test_1": "error"}, (
        "the test's own teardown was deferred past its verdict: %r" % done0)
    result.assert_outcomes(passed=2, errors=1)


# ---------------------------------------------------------------- a nodeid handed over twice

REPEATABLE = """
    import os
    import pytest

    _N = [0]

    @pytest.fixture
    def res():
        return "ok"

    def test_flaky(res):
        _N[0] += 1
        open(os.path.join(os.path.dirname(__file__), "ran-%d.marker" % _N[0]), "a").close()
        assert res == "ok"
"""


def _ran(pytester):
    return len(list(pytester.path.glob("ran-*.marker")))


def test_the_same_nodeid_in_two_consecutive_waves_runs_twice_and_passes_twice(pytester):
    """A healthy test handed to a live session twice must run twice and pass twice.

    The precondition is already in the backend: reclaim_dead_executors $pushes a dead child's
    running list back to the queue without $addToSet, so two concurrent reclaims of one child leave
    the id twice, and the assignment step hands it out as a flat slice. At the default batch of one
    that is two consecutive single-nodeid waves into the same session.

    Measured before the fix: wave 0 passed, wave 1 FAILED with `KeyError: 'res'`, and the test body
    never ran at all — the item was still on the setup stack, so pytest saw nothing to set up and
    the fixture was never re-created.
    """
    pytester.makepyfile(test_suite=REPEATABLE)
    control = pytester.path / "control"
    control.mkdir()

    nid = "test_suite.py::test_flaky"
    feeder = _feed(control, [[nid], [nid]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    for n in (0, 1):
        done = json.loads((control / ("wave-%d.done.json" % n)).read_text(encoding="utf-8"))
        assert _rows(done, "results") == {nid: "passed"}, "wave %d: %r" % (n, done)
    assert _ran(pytester) == 2, "the test body ran %d times, not twice" % _ran(pytester)
    result.assert_outcomes(passed=2)


def test_a_nodeid_repeated_inside_ONE_wave_does_not_clobber_its_own_verdict(pytester):
    """Results are keyed by nodeid, so a duplicate inside one wave publishes ONE row for TWO runs —
    the second overwriting the first. Measured before the fix: pytest reported "1 failed, 1 passed"
    and the done file reported the nodeid as failed, i.e. a good run was published as the failure of
    its own duplicate. Deduplicating the wave is the only reading that can be reported honestly."""
    pytester.makepyfile(test_suite=REPEATABLE)
    control = pytester.path / "control"
    control.mkdir()

    nid = "test_suite.py::test_flaky"
    feeder = _feed(control, [[nid, nid]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done0 = json.loads((control / "wave-0.done.json").read_text(encoding="utf-8"))
    assert len(done0["results"]) == 1
    assert _rows(done0, "results") == {nid: "passed"}
    assert _ran(pytester) == 1
    result.assert_outcomes(passed=1)


# ---------------------------------------------------------------- the loop cannot be trapped

def test_an_unparseable_wave_file_still_honours_stop(pytester):
    """`stop` and the idle deadline sat BELOW the "does the wave file exist?" branch, and the
    unparseable case ended in `continue`. So a wave-N.json that exists and never parses re-entered
    that branch every iteration and both exits were unreachable: the executor spun at 50Hz holding
    its node until the build timed out. Measured with the idle deadline set to 2s — still alive at
    20s, killed at 25s.

    Not reachable through our own client, which renames into place; reachable through a leftover in
    a reused control directory, a hand-edited file, or wave-N.json being a directory.
    """
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()
    (control / "wave-0.json").write_text('{"nodeids": ["test_suite.py::tes', encoding="utf-8")
    (control / "stop").write_text("", encoding="utf-8")

    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=40)

    assert result.ret == 0
    assert not (control / "wave-0.done.json").exists()


def test_looponfail_is_disarmed_for_a_SESSION_too(pytester, monkeypatch):
    """xdist implements -f in pytest_cmdline_main, which never returns: it re-runs failures forever.
    The guard against it is the only door that cannot be closed from pytest_configure — and it was
    reading a flag that pytest_configure is what SETS.

    Measured against pytest 9.1.1: `_pytest.main.pytest_cmdline_main` IS `wrap_session(config,
    _main)`, and `_do_configure()` is called by wrap_session. So configure runs INSIDE this hook,
    strictly after the tryfirst guard has read the flag and returned. A session-mode run with -f
    never returned and had to be killed at 25s, holding its node and reporting nothing — with the
    directory delivered either way, env var or option. The existing neutralisation test passes
    --testhide-batch explicitly, which is the one spelling that already worked.
    """
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=SUITE)
    control = pytester.path / "control"
    control.mkdir()
    (control / "stop").write_text("", encoding="utf-8")
    monkeypatch.setenv("TESTHIDE_SESSION_DIR", str(control))

    result = pytester.runpytest_subprocess("test_suite.py", "-f", timeout=40)

    assert result.ret == 0
    result.stdout.fnmatch_lines(["*ignoring -f/--looponfail*"])


# ---------------------------------------------------------------- the numbers are real numbers

def test_a_slow_test_reports_a_larger_duration_than_a_fast_one(pytester):
    """The only assertion about durations was `"duration" in r`, which is true of a key written
    unconditionally — measured: replacing the value with a literal 0.0 left the whole suite green.
    Every other test that touches a duration hands the reader a ready-made number, so none of them
    can fail on a producer that emits zero.

    Durations are what the scheduler orders batches by, so a producer that quietly emits zero
    degrades longest-processing-time ordering to collection order with nothing to show for it.
    """
    pytester.makepyfile(test_suite="""
        import time

        def test_slow(): time.sleep(0.3)
        def test_fast(): pass
    """)
    control = pytester.path / "control"
    control.mkdir()

    feeder = _feed(control, [["test_suite.py::test_slow", "test_suite.py::test_fast"]])
    pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=120)
    feeder.join(timeout=30)

    done0 = json.loads((control / "wave-0.done.json").read_text(encoding="utf-8"))
    by_id = {r["nodeid"]: r["duration"] for r in done0["results"]}
    slow = by_id["test_suite.py::test_slow"]
    fast = by_id["test_suite.py::test_fast"]
    assert slow > 0.2, "the slow test reported %r" % slow
    assert slow > fast * 10, "slow=%r fast=%r" % (slow, fast)


# ---------------------------------------------------------------- the session outlives the agent

def _ctl(pytester):
    control = pytester.path / "control"
    control.mkdir()
    return control


def test_the_session_publishes_its_own_pid_before_it_collects(pytester):
    """The handshake, and the only handle that reaches the interpreter.

    The client starts cmd.exe, which starts python; every kill it can perform walks down from the
    shell. Once the shell has exited -- or was never assigned to the job object, which the
    interactive launch path can skip -- this file is the only way to reach the process that is
    holding the application under test.

    At CONFIGURE time, not in the wave loop: on a large suite, collection is the longest part of a
    session's startup, and the client needs to know "it started" during exactly that window.
    """
    pytester.makepyfile(test_suite=SUITE)
    control = _ctl(pytester)
    (control / "stop").write_text("", encoding="utf-8")

    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=40)

    assert result.ret == 0
    published = json.loads((control / "session-pid.json").read_text(encoding="utf-8"))
    assert isinstance(published["pid"], int) and published["pid"] > 0
    assert published["plugin_version"] and published["protocol"] == 1


def test_an_orphaned_session_exits_without_reporting_anything(pytester, monkeypatch):
    """The guarantee that survives the agent dying.

    A persistent session outlives every batch, so for the first time the agent's own `finally` is
    not enough: kill the agent and nothing runs at all. The only code that still executes is this
    process, so the guarantee has to live here.

    And it must be a HARD exit. A `break` would fall into the results loop, which emits a verdict
    for every item of the wave -- an item that never ran has no phase reports, so _verdict returns
    'missing', WaveJUnit renders <error>, and the backend reads <error> as FAILED. One stalled
    heartbeat would publish every remaining test of the wave as a failure, and those verdicts are
    final: a later truthful row for the same nodeid is dropped as a duplicate. So the assertion
    that matters is the NEGATIVE one -- nothing was written.
    """
    from testhide_plugin import plugin as tp

    pytester.makepyfile(test_suite=SUITE)
    control = _ctl(pytester)
    # A pid that cannot exist, and a heartbeat old enough to be stale by any reading.
    monkeypatch.setenv("TESTHIDE_SESSION_OWNER_PID", "999999999")
    hb = control / "owner-alive"
    hb.write_text("", encoding="utf-8")
    os.utime(str(hb), (time.time() - 10_000, time.time() - 10_000))

    feeder = _feed(control, [A[:2]], deadline=3.0)
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=60)
    feeder.join(timeout=30)

    assert result.ret == 3, "expected the hard-exit code, got %r" % result.ret
    assert not (control / "wave-0.done.json").exists(), (
        "an orphaned session published verdicts; unrun tests would be reported as failures")
    assert not (control / "session-ended.json").exists()


def test_a_live_owner_is_never_mistaken_for_a_dead_one(pytester, monkeypatch):
    """The load-bearing negative. A false DEATH abandons a running wave and hands its nodeids to
    another node; a false ALIVE only costs a timeout. The check must therefore be hard to trigger:
    a fresh heartbeat alone keeps the session, whatever the pid says."""
    pytester.makepyfile(test_suite=SUITE)
    control = _ctl(pytester)
    monkeypatch.setenv("TESTHIDE_SESSION_OWNER_PID", "999999999")
    (control / "owner-alive").write_text("", encoding="utf-8")     # written just now

    result, done = None, None
    feeder = _feed(control, [A[:2], A[2:]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=60)
    feeder.join(timeout=30)

    result.assert_outcomes(passed=3)
    assert (control / "wave-1.done.json").exists()


def test_a_missing_heartbeat_file_is_not_evidence_of_death(pytester, monkeypatch):
    """An older client writes no heartbeat at all. Absence must read as "unknown", not "gone" --
    otherwise upgrading the plugin alone would kill every session on the farm."""
    pytester.makepyfile(test_suite=SUITE)
    control = _ctl(pytester)
    monkeypatch.setenv("TESTHIDE_SESSION_OWNER_PID", "999999999")

    feeder = _feed(control, [A[:2]])
    result = pytester.runpytest_subprocess(
        "test_suite.py", "--testhide-session-dir", str(control), timeout=60)
    feeder.join(timeout=30)

    result.assert_outcomes(passed=2)
    assert (control / "wave-0.done.json").exists()


def test_the_owner_check_is_the_first_statement_of_the_item_loop_body():
    """Placement, asserted over the AST because it cannot be observed from outside.

    The item loop ends with a `break` on shouldfail/shouldstop, so a check placed after the
    protocol call is skipped by every early exit -- the same trailing-await shape that has bitten
    this codebase before. First statement is the only placement nothing can skip.
    """
    import ast
    import inspect
    from testhide_plugin import plugin as tp

    tree = ast.parse(inspect.getsource(tp))
    run = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == 'run')
    loop = next(n for n in ast.walk(run)
                if isinstance(n, ast.For)
                and isinstance(n.iter, ast.Call)
                and getattr(n.iter.func, 'id', '') == 'enumerate')

    first = loop.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert getattr(first.value.func, 'attr', None) == '_abandon_if_orphaned', (
        "the owner check is not the first statement of the item loop; the break at the foot of the "
        "loop would skip it")
