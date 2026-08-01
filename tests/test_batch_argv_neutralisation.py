# -*- coding: utf-8 -*-
"""D-0801 / P2 — `--testhide-batch` makes a scheduled batch run exactly what it was given.

TestHide's distributed strategy hands each executor an explicit list of nodeids, then reuses the
JOB'S OWN build script to run them. The script belongs to the customer, so whatever it carries
comes along — and two of those things silently break the scheduler:

  -m / -k   deselect tests the scheduler explicitly assigned. They are not re-queued (no result was
            reported, and nothing failed), so they sit in the queue until the heartbeat sweep hands
            them to another executor, which deselects them too. The suite ends with tests
            permanently unaccounted for.

  -n        fans the batch across worker processes, which re-pays the class-scoped fixture per
            worker, runs several instances of a single-instance desktop app, and puts several
            workers on one shared Steam account.

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

CLASS_FIXTURE = """
    import os, pytest

    class TestHeavy:
        @pytest.fixture(scope="class", autouse=True)
        def heavy(self, request):
            # One line per PROCESS that pays the fixture.
            with open(os.path.join(request.config.rootdir, "setups.log"), "a") as fh:
                fh.write("%d\\n" % os.getpid())

        def test_1(self): assert True
        def test_2(self): assert True
        def test_3(self): assert True
        def test_4(self): assert True
"""


def _ini(pytester, body):
    pytester.makefile(".ini", pytest="[pytest]\n" + body + "\n")


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


def test_the_override_is_announced(pytester):
    """Silently rewriting the customer's own arguments is the kind of help that costs an afternoon
    when it turns out to be wrong. Every override says so in the build log."""
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("-m", "smoke", "--testhide-batch")

    result.stdout.fnmatch_lines(["*[[]testhide[]] ignoring -m*"])


def test_an_ordinary_run_is_untouched(pytester):
    """The flag is opt-in. Without it, -m must behave exactly as pytest intends — this guard must
    never leak into a customer's normal build."""
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("-m", "smoke")

    result.assert_outcomes(passed=1, deselected=2)
    assert not any("[testhide] ignoring" in ln for ln in result.outlines)


# --------------------------------------------------------------------------- xdist

def _setup_count(pytester):
    log = pytester.path / "setups.log"
    if not log.exists():
        return 0
    return len({ln for ln in log.read_text().split() if ln})


def test_xdist_really_does_re_pay_the_class_fixture(pytester):
    """Second non-vacuity anchor, and the measurement the whole guard exists for: 4 workers, 4
    setups. A class-scoped fixture is amortised per PROCESS, so fanning a batch out multiplies the
    exact cost this scheduler was built to avoid."""
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)

    result = pytester.runpytest_subprocess("-n", "4")

    result.assert_outcomes(passed=4)
    assert _setup_count(pytester) > 1, "xdist did not actually fan out; this test proves nothing"


def test_the_batch_flag_collapses_xdist_to_one_process(pytester):
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)

    result = pytester.runpytest_subprocess("-n", "4", "--testhide-batch")

    result.assert_outcomes(passed=4)
    assert _setup_count(pytester) == 1, (
        "the class fixture was paid %d times — xdist was not disabled" % _setup_count(pytester))


@pytest.mark.parametrize("argv", [
    ["-n", "4"],
    ["-n", "auto"],
    ["-n", "4", "--dist", "loadscope"],
    ["-n", "4", "--dist", "worksteal"],
    ["-n", "4", "-p", "xdist"],
])
def test_every_hostile_xdist_shape_collapses(pytester, argv):
    """The customer's script is arbitrary. Each of these reaches xdist by a different route."""
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)

    result = pytester.runpytest_subprocess(*argv, "--testhide-batch")

    result.assert_outcomes(passed=4)
    assert _setup_count(pytester) == 1, "%s escaped the guard" % (argv,)


def test_xdist_reached_through_ini_addopts_is_also_collapsed(pytester):
    """argv is not the only door. `-n` in the project's own pytest.ini reaches xdist without ever
    appearing on the command line the client builds, so a guard that inspected argv would miss it.
    """
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)
    _ini(pytester, "addopts = -n 4")

    result = pytester.runpytest_subprocess("--testhide-batch")

    result.assert_outcomes(passed=4)
    assert _setup_count(pytester) == 1


def test_ini_addopts_xdist_really_does_fan_out_without_the_flag(pytester):
    """Non-vacuity for the test above."""
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)
    _ini(pytester, "addopts = -n 4")

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=4)
    assert _setup_count(pytester) > 1


def test_dist_no_alone_would_not_have_been_enough(pytester):
    """Pins the measurement that shaped the implementation, so nobody simplifies it back.

    The obvious fix is `--dist no`. It does not work: xdist's own plugin OVERRIDES dist when -n is
    present, so the run still forks. numprocesses and tx have to be zeroed as well. Reasoning said
    one line would do; measuring said otherwise.
    """
    pytest.importorskip("xdist")
    pytester.makepyfile(test_suite=CLASS_FIXTURE)

    result = pytester.runpytest_subprocess("-n", "4", "--dist", "no")

    result.assert_outcomes(passed=4)
    assert _setup_count(pytester) > 1, (
        "--dist no alone now suffices; the extra numprocesses/tx zeroing may be simplified")


def test_a_batch_without_xdist_installed_still_runs(pytester):
    """The guard must not require xdist. Most customers do not have it, and touching an option that
    does not exist would break every one of their batches."""
    pytester.makepyfile(test_suite=MARKED)

    result = pytester.runpytest_subprocess("--testhide-batch", "-p", "no:cacheprovider")

    result.assert_outcomes(passed=3)
