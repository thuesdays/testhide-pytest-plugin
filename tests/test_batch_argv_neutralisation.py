# -*- coding: utf-8 -*-
"""D-0801 / P2 — `--testhide-batch` makes a scheduled batch run exactly what it was given.

TestHide's distributed strategy hands each executor an explicit list of nodeids, then reuses the
JOB'S OWN build script to run them. The script belongs to the customer, so whatever it carries comes
along — and a lot of ordinary pytest flags silently break the scheduler in the SAME way:

  a nodeid that was assigned, but neither runs, nor reports, nor fails.

Such a test is not re-queued (no result was reported, and nothing failed), so it sits in the queue
until the heartbeat sweep hands it to another executor, which drops it too. The suite ends with
tests permanently unaccounted for.

The first version of this file covered three doors (-m, -k, -n). RA-3 measured four more that
produce the identical symptom — --deselect, --lf, --sw and -x/--maxfail — plus -f, which does not
merely drop tests but never returns at all. Each has a non-vacuity anchor here: the anchor proves
the flag really does break a batch, so the guard test next to it is proving something.

These tests drive real pytest runs through `pytester`, because the thing under test is the
interaction with pytest's own option handling — the exact place where reasoning alone was wrong
before (see the --dist note below).
"""
from __future__ import annotations

import pytest


MARKED = """
    import pytest

    @pytest.mark.smoke
    def test_a(): assert True

    def test_b(): assert True
    def test_c(): assert True
"""

# One test fails, deliberately and always. Every "stops early / remembers failures" flag needs a
# failure to act on.
ONE_FAILS = """
    def test_1(): assert False, "planted"
    def test_2(): assert True
    def test_3(): assert True
    def test_4(): assert True
"""

CLASS_FIXTURE = """
    import os, pytest

    class TestHeavy:
        @pytest.fixture(scope="class", autouse=True)
        def heavy(self, request):
            # One FILE per process that pays the fixture. Not one line in a shared file: two
            # concurrent workers appending to the same path lost writes on Windows, which is what
            # kept the last of these anchors flaky (1 failure in ~6 runs) even after the
            # distribution itself had been made deterministic.
            open(os.path.join(str(request.config.rootdir),
                              "setup-%d.marker" % os.getpid()), "a").close()

        def test_1(self): assert True
        def test_2(self): assert True
        def test_3(self): assert True
        def test_4(self): assert True
"""

# Counts PROCESSES, not fixture payments.
#
# RA-3 found the fixture counter made the "xdist really does fan out" anchors FLAKY: with --dist
# load and 4 tests over 4 workers, one worker can legitimately take all four, and then exactly one
# process pays the fixture. Measured 2 failures in 12 consecutive runs — and those flakes produced
# FALSE KILLS in a mutation run, which is worse than the flake itself.
#
# Every worker runs pytest_configure, whatever the scheduler decides to send it, so this is
# deterministic where the fixture count is not.
CONFTEST_PIDS = """
    import os

    def pytest_configure(config):
        open(os.path.join(str(config.rootdir), "proc-%d.marker" % os.getpid()), "a").close()
"""


def _ini(pytester, body):
    pytester.makefile(".ini", pytest="[pytest]\n" + body + "\n")


def _count(pytester, prefix):
    """One marker FILE per process — see CLASS_FIXTURE for why not one line per process."""
    return len(list(pytester.path.glob("%s-*.marker" % prefix)))


def _setup_count(pytester):
    return _count(pytester, "setup")


def _proc_count(pytester):
    return _count(pytester, "proc")


# --------------------------------------------------------------------------- marker / keyword

def test_a_marker_expression_would_drop_scheduled_tests(pytester):
    """Non-vacuity anchor: without the flag, -m really does deselect. If it did not, everything
    below would be proving nothing."""
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("-m", "smoke")

    result.assert_outcomes(passed=1, deselected=2)


def test_the_batch_flag_neutralises_a_marker_expression(pytester):
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("-m", "smoke", "--testhide-batch")

    result.assert_outcomes(passed=3)


def test_the_batch_flag_neutralises_a_keyword_expression(pytester):
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("-k", "test_a", "--testhide-batch")

    result.assert_outcomes(passed=3)


def test_a_marker_expression_from_ini_addopts_is_also_neutralised(pytester):
    """argv is not the only source. The client neutralises ini addopts for DISCOVERY with
    `-o addopts=`, but execution deliberately keeps the customer's addopts — so the guard has to
    hold here rather than assuming a clean command line."""
    pytester.makepyfile(test_suite=MARKED)
    _ini(pytester, "addopts = -m smoke")

    result = pytester.runpytest_subprocess("--testhide-batch")

    result.assert_outcomes(passed=3)


def test_an_ordinary_run_is_untouched(pytester):
    """The flag is opt-in. Without it, -m must behave exactly as pytest intends — this guard must
    never leak into a customer's normal build."""
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("-m", "smoke")

    result.assert_outcomes(passed=1, deselected=2)
    assert not any("[testhide] ignoring" in ln for ln in result.outlines)


# ------------------------------------------------------- the four doors RA-3 found still open

def test_deselect_would_drop_a_scheduled_test(pytester):
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("--deselect", "test_suite.py::test_b")

    result.assert_outcomes(passed=2, deselected=1)


def test_the_batch_flag_neutralises_deselect(pytester):
    """Identical symptom to -m, reached by a flag the first version of this guard did not know
    about. rc was 0 either way, so nothing upstream could notice."""
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess(
        "--deselect", "test_suite.py::test_b", "--testhide-batch")

    result.assert_outcomes(passed=3)


def test_last_failed_would_drop_most_of_the_batch(pytester):
    """The worst of the four: --lf with a warm cache runs ONE of four assigned nodeids.

    Asserted as "three produced no result", not as "three were deselected". Modern pytest reports
    --lf by narrowing collection rather than by counting deselections, so a deselected= assertion
    reads 0 and would make this anchor pass for the wrong reason. What matters to the scheduler is
    identical either way: three nodeids it assigned came back with nothing.
    """
    pytester.makepyfile(test_suite=ONE_FAILS)
    pytester.runpytest_subprocess()                      # seed .pytest_cache

    result = pytester.runpytest_subprocess("--lf")

    result.assert_outcomes(failed=1, passed=0)


def test_the_batch_flag_neutralises_last_failed(pytester):
    pytester.makepyfile(test_suite=ONE_FAILS)
    pytester.runpytest_subprocess()

    result = pytester.runpytest_subprocess("--lf", "--testhide-batch")

    result.assert_outcomes(failed=1, passed=3)


def test_maxfail_would_leave_the_rest_of_the_batch_with_no_result(pytester):
    """-x is not a filter, which is why it was missed. It TRUNCATES: the junit the backend reads
    then contains a testcase for everything up to the failure and nothing after it, and the backend
    cannot tell "never ran" from "no result reported"."""
    pytester.makepyfile(test_suite=ONE_FAILS)

    result = pytester.runpytest_subprocess("-x")

    result.assert_outcomes(failed=1)          # tests 2-4 produce no row at all


def test_the_batch_flag_neutralises_maxfail(pytester):
    pytester.makepyfile(test_suite=ONE_FAILS)

    result = pytester.runpytest_subprocess("-x", "--testhide-batch")

    result.assert_outcomes(failed=1, passed=3)


def test_maxfail_from_ini_addopts_is_also_neutralised(pytester):
    pytester.makepyfile(test_suite=ONE_FAILS)
    _ini(pytester, "addopts = --maxfail=1")

    result = pytester.runpytest_subprocess("--testhide-batch")

    result.assert_outcomes(failed=1, passed=3)


def test_stepwise_would_truncate_and_then_deselect(pytester):
    pytester.makepyfile(test_suite=ONE_FAILS)

    result = pytester.runpytest_subprocess("--sw")

    result.assert_outcomes(failed=1)


def test_the_batch_flag_neutralises_stepwise(pytester):
    pytester.makepyfile(test_suite=ONE_FAILS)

    result = pytester.runpytest_subprocess("--sw", "--testhide-batch")

    result.assert_outcomes(failed=1, passed=3)


def test_every_override_announces_itself(pytester):
    """Silently rewriting the customer's own arguments is the kind of help that costs an afternoon
    when it turns out to be wrong. RA-3: only the -m line was ever asserted, and deleting the other
    announcements left the suite green — while one shape (--dist with --tx and no -n) rewrote the
    arguments with NO log line at all, because the old code gated the message on `numprocesses`."""
    pytester.makepyfile(test_suite=ONE_FAILS)

    result = pytester.runpytest_subprocess(
        "-m", "smoke", "-k", "test_1", "--deselect", "test_suite.py::test_2",
        "--sw", "-x", "--testhide-batch")

    for expected in ("-m", "-k", "--deselect", "--sw", "--maxfail/-x"):
        result.stdout.fnmatch_lines(["*[[]testhide[]] ignoring %s*" % expected])


# --------------------------------------------------------------------------- looponfail

def test_looponfail_is_disarmed_instead_of_hanging_forever(pytester):
    """The one door that cannot be closed from pytest_configure.

    xdist implements -f in `pytest_cmdline_main` (looponfail.py:41-47), which runs BEFORE any
    pytest_configure and enters `while 1` — measured, `pytest --testhide-batch -f` produced no
    output at all and had to be killed at 20s. On a farm that is an executor holding its node until
    the job timeout while reporting nothing for every nodeid it was given.

    A test that hangs is a test that hangs CI, so this one is bounded: if the guard regresses it
    fails on the timeout rather than running forever.
    """
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("-f", "--testhide-batch", timeout=90)

    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(["*[[]testhide[]] ignoring -f/--looponfail*"])


# --------------------------------------------------------------------------- xdist

def test_xdist_really_does_fan_out(pytester):
    """Non-vacuity anchor, counted by PROCESS. See CONFTEST_PIDS for why not by fixture payment."""
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)
    pytester.makeconftest(CONFTEST_PIDS)

    result = pytester.runpytest_subprocess("-n", "4")

    result.assert_outcomes(passed=4)
    assert _proc_count(pytester) > 1, "xdist did not actually fan out; this test proves nothing"


def test_xdist_really_does_re_pay_the_class_fixture(pytester):
    """The measurement the whole guard exists for, made deterministic.

    --dist each sends EVERY test to EVERY worker, so two workers pay the class fixture twice with no
    dependence on how the scheduler happens to split the work. With --dist load this same claim was
    flaky (see CONFTEST_PIDS).
    """
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)

    result = pytester.runpytest_subprocess("--dist", "each", "--tx", "popen", "--tx", "popen")

    result.assert_outcomes(passed=8)                  # every test on every worker
    assert _setup_count(pytester) == 2, (
        "expected one class-fixture payment per worker, got %d" % _setup_count(pytester))


def test_the_batch_flag_collapses_xdist_to_one_process(pytester):
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)
    pytester.makeconftest(CONFTEST_PIDS)

    result = pytester.runpytest_subprocess("-n", "4", "--testhide-batch")

    result.assert_outcomes(passed=4)
    assert _proc_count(pytester) == 1, "xdist still forked %d processes" % _proc_count(pytester)
    assert _setup_count(pytester) == 1, (
        "the class fixture was paid %d times — xdist was not disabled" % _setup_count(pytester))


def test_tx_without_n_is_also_collapsed(pytester):
    """`--tx popen --tx popen` reaches distribution mode without setting numprocesses at all —
    xdist computes `_is_distribution_mode = dist != "no" and bool(tx)`. Every hostile shape in the
    parametrisation below carries -n, so nothing covered this until RA-3's mutation run found a
    surviving mutant that gated the whole override block on numprocesses: measured, every scheduled
    nodeid ran TWICE, in two concurrent processes — the exact single-instance / one-Steam-account
    breakage, on a farm where that means two clients fighting over one desktop app."""
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)

    result = pytester.runpytest_subprocess(
        "--dist", "each", "--tx", "popen", "--tx", "popen", "--testhide-batch")

    result.assert_outcomes(passed=4)                  # not 8: one process, each test once
    assert _setup_count(pytester) == 1


@pytest.mark.parametrize("argv", [
    ["-n", "4"],
    ["-n", "auto"],
    ["-n", "4", "--dist", "loadscope"],
    ["-n", "4", "--dist", "worksteal"],
    ["-n", "4", "-p", "xdist"],
])
def test_every_hostile_xdist_shape_collapses(pytester, argv):
    """The customer's script is arbitrary. Each of these reaches xdist by a different route.

    Asserted on the PROCESS count as well as the fixture count: RA-3 measured that --dist loadscope
    groups a whole class onto one worker, so the fixture-count assertion alone was satisfied by
    loadscope's own semantics while four workers were still forked — and four workers is what breaks
    the single-instance app and the shared account, whoever pays the fixture.
    """
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)
    pytester.makeconftest(CONFTEST_PIDS)

    result = pytester.runpytest_subprocess(*argv, "--testhide-batch")

    result.assert_outcomes(passed=4)
    assert _proc_count(pytester) == 1, "%s still forked %d processes" % (argv, _proc_count(pytester))
    assert _setup_count(pytester) == 1, "%s escaped the guard" % (argv,)


def test_xdist_reached_through_ini_addopts_is_also_collapsed(pytester):
    """argv is not the only door. `-n` in the project's own pytest.ini reaches xdist without ever
    appearing on the command line the client builds, so a guard that inspected argv would miss it.
    """
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)
    pytester.makeconftest(CONFTEST_PIDS)
    _ini(pytester, "addopts = -n 4")

    result = pytester.runpytest_subprocess("--testhide-batch")

    result.assert_outcomes(passed=4)
    assert _proc_count(pytester) == 1


def test_ini_addopts_xdist_really_does_fan_out_without_the_flag(pytester):
    """Non-vacuity for the test above."""
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)
    pytester.makeconftest(CONFTEST_PIDS)
    _ini(pytester, "addopts = -n 4")

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=4)
    assert _proc_count(pytester) > 1


def test_dist_no_in_the_script_is_overridden_by_xdist_itself(pytester):
    """Pins the measurement that shaped the implementation, so nobody "simplifies" it back into the
    build script.

    The obvious fix is to put `--dist no` in the command the client builds. It does not work: xdist's
    own plugin overrides dist when -n is present, so the run still forks. Setting config.option.dist
    from pytest_configure DOES work, because that runs after the override.

    Renamed after RA-3: the old name and failure message said "--dist no alone would not have been
    enough", which conflated this measurement (about the SCRIPT) with a different question (whether
    the extra numprocesses/tx zeroing inside the guard is load-bearing). It never passed
    --testhide-batch at all, so it could not have measured the guard's internals.
    """
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)
    pytester.makeconftest(CONFTEST_PIDS)

    result = pytester.runpytest_subprocess("-n", "4", "--dist", "no")

    result.assert_outcomes(passed=4)
    assert _proc_count(pytester) > 1, (
        "xdist no longer overrides --dist when -n is present; the guard could move to the script")


def test_a_batch_without_xdist_installed_still_runs(pytester):
    """The guard must not require xdist. Most customers do not have it, and touching an option that
    does not exist would break every one of their batches.

    RA-3: this never created its condition — `-p no:cacheprovider` does not remove xdist, so the run
    had xdist loaded the whole time (corroborated by a surviving mutant that deleted the hasattr
    check). `-p no:xdist` cannot be used either: it removes the option definitions, so a script
    containing -n dies at argument parsing with rc=4. The honest way to prove the guard tolerates a
    missing option is to call it with an option namespace that does not have one.
    """
    import argparse

    from testhide_plugin.plugin import _neutralise_batch_argv

    class _Config:
        option = argparse.Namespace(markexpr='smoke', keyword='', testhide_batch=True)

    config = _Config()
    _neutralise_batch_argv(config)                    # must not raise on absent dist/numprocesses/tx

    assert config.option.markexpr == ''
    assert not hasattr(config.option, 'dist'), "the guard invented an option pytest never defined"


def test_a_batch_on_a_plain_pytest_still_runs(pytester):
    """And the end-to-end half of the same claim, as far as this environment allows."""
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("--testhide-batch")

    result.assert_outcomes(passed=3)


# --------------------------------------------------------------------------- quarantine

QUARANTINE_SUITE = """
    def test_1(): assert True
    def test_2(): assert True
    def test_3(): assert True
"""


def _quarantine(pytester, *nodeids):
    path = pytester.path / "q.txt"
    path.write_text("\n".join(nodeids) + "\n")
    return str(path)


def test_quarantine_still_deselects_on_an_ordinary_run(pytester):
    """Unchanged behaviour, pinned so the batch branch below cannot quietly become the only one.
    An ordinary run has no scheduler waiting on those nodeids, and deselecting keeps the summary
    clean."""
    pytester.makepyfile(test_suite=QUARANTINE_SUITE)
    qfile = _quarantine(pytester, "test_suite.py::test_2")

    result = pytester.runpytest_subprocess("--quarantine-file", qfile)

    result.assert_outcomes(passed=2, deselected=1)


def test_quarantine_inside_a_batch_reports_a_skip_instead_of_dropping(pytester):
    """RA-3. The batch guard neutralises pytest's OWN deselection doors, and then the plugin's own
    quarantine hook produced the exact outcome the guard exists to eliminate: an assigned nodeid
    that neither runs, nor reports, nor fails. Measured before the fix: 2 passed, 1 deselected, and
    a junit containing only two of the three nodeids this executor was told to run.

    A skip is still a quarantine (the test does not execute) but it is a RESULT: the scheduler
    accounts for the nodeid, nothing is re-queued forever, and the report says why.
    """
    pytester.makepyfile(test_suite=QUARANTINE_SUITE)
    qfile = _quarantine(pytester, "test_suite.py::test_2")

    result = pytester.runpytest_subprocess("--quarantine-file", qfile, "--testhide-batch")

    result.assert_outcomes(passed=2, skipped=1, deselected=0)


def test_the_skip_says_why(pytester):
    pytester.makepyfile(test_suite=QUARANTINE_SUITE)
    qfile = _quarantine(pytester, "test_suite.py::test_2")

    result = pytester.runpytest_subprocess(
        "--quarantine-file", qfile, "--testhide-batch", "-rs")

    result.stdout.fnmatch_lines(["*testhide: quarantined*"])


def test_a_class_prefix_quarantine_still_matches_inside_a_batch(pytester):
    """The prefix rule (quarantine a class, get every test in it) has to survive the new branch."""
    pytester.makepyfile(test_suite="""
        class TestThing:
            def test_a(self): assert True
            def test_b(self): assert True

        def test_free(): assert True
    """)
    qfile = _quarantine(pytester, "test_suite.py::TestThing")

    result = pytester.runpytest_subprocess("--quarantine-file", qfile, "--testhide-batch")

    result.assert_outcomes(passed=1, skipped=2)
