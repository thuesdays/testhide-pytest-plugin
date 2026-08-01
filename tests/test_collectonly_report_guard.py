# -*- coding: utf-8 -*-
"""D-0801 / P0 — a --collect-only run must not destroy the report.

`pytest_sessionfinish` fires for a collect-only run exactly as for a real one. No test executed,
so the plugin's temp dir is empty, and the merge then `os.replace()`s the existing junittests.xml
with a document containing tests="0". The previous, real report is gone.

This is a live defect today for anyone who runs `--collect-only` against a job that also passes
`--report-xml`. It becomes load-bearing for the TPS discovery pass, which deliberately reuses the
job's own build script — and therefore its `--report-xml` argument — verbatim.

The tests below drive real pytest runs via `pytester`; they do not call the hook directly, because
the thing being verified is the interaction with pytest's option handling.
"""
from __future__ import annotations

import pytest
import xml.etree.ElementTree as ET


SUITE = """
    import pytest

    class TestAlpha:
        def test_one(self): assert True
        def test_two(self): assert True

    def test_top_level(): assert True
"""


def _tests_attr(xml_path):
    """Read the <testsuite tests="..."> count, tolerating a testsuites wrapper."""
    root = ET.parse(str(xml_path)).getroot()
    node = root if root.tag == "testsuite" else root.find("testsuite")
    assert node is not None, "no <testsuite> element in %s" % xml_path
    return int(node.get("tests", "-1"))


def test_a_real_run_writes_the_report(pytester):
    """Non-vacuity anchor. Everything below compares against this; if the plugin never wrote a
    report in the first place, 'the report survived' would be trivially true and meaningless."""
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest_subprocess("--report-xml=junittests.xml")

    result.assert_outcomes(passed=3)
    report = pytester.path / "junittests.xml"
    assert report.exists(), "the plugin did not produce a report at all"
    assert _tests_attr(report) == 3


def test_collect_only_does_not_clobber_an_existing_report(pytester):
    """THE regression. Run for real, then run --collect-only against the SAME report path and
    assert the real results are still there."""
    pytester.makepyfile(test_suite=SUITE)
    report = pytester.path / "junittests.xml"

    pytester.runpytest_subprocess("--report-xml=junittests.xml")
    assert _tests_attr(report) == 3, "precondition failed: the first run did not write 3 tests"
    before = report.read_bytes()

    result = pytester.runpytest_subprocess(
        "--report-xml=junittests.xml", "--collect-only")

    assert result.ret == 0, "collect-only run failed: %s" % result.outlines[-5:]
    assert _tests_attr(report) == 3, (
        "the collect-only run overwrote the report (tests=%d) — the previous real results are gone"
        % _tests_attr(report))
    assert report.read_bytes() == before, "report bytes changed during a collect-only run"


def test_collect_only_still_collects(pytester):
    """The guard must not turn discovery into a no-op: collection itself has to keep working,
    because the TPS discovery pass depends on it.

    Kept deliberately, with its limits stated: a re-audit showed this test survives EVERY mutation
    of the guard (removing it, misnaming the option, inverting it, `if True: return`). That is not
    a flaw in the assertions — it is structural. The guard lives in pytest_sessionfinish, which runs
    after collection has already been reported, so no placement there can break collection. The test
    therefore proves the property for the shipped design and would only discriminate against the
    collection-time alternative that was considered and rejected. It is a regression net for that
    redesign, not evidence about the current one — do not cite it as such.
    """
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest_subprocess(
        "--report-xml=junittests.xml", "--collect-only", "-q")

    assert result.ret == 0
    collected = [ln.strip() for ln in result.outlines if "::" in ln and not ln.startswith("<")]
    assert any("TestAlpha::test_one" in c for c in collected), (
        "collect-only produced no nodeids: %s" % result.outlines[:15])
    assert len(collected) >= 3


# ---------------------------------------------------------------------------
# The other doors into wrap_session.
#
# pytest calls wrap_session — and therefore pytest_sessionstart/pytest_sessionfinish — from four
# places, not one. Three of them run a full session that executes nothing, and none of them sets
# `collectonly`. The first version of this guard whitelisted --collect-only alone and every one of
# these still destroyed the report.
# ---------------------------------------------------------------------------

def _seed_real_report(pytester):
    """Write a genuine 3-test report, then hand back its bytes for comparison."""
    pytester.makepyfile(test_suite=SUITE)
    pytester.runpytest_subprocess("--report-xml=junittests.xml")
    report = pytester.path / "junittests.xml"
    assert _tests_attr(report) == 3, "precondition: the seeding run did not write 3 tests"
    return report, report.read_bytes()


@pytest.mark.parametrize("mode", ["--fixtures", "--fixtures-per-test", "--cache-show"])
def test_listing_modes_do_not_destroy_the_report(pytester, mode):
    """--fixtures and --fixtures-per-test go through _showfixtures_main / _show_fixtures_per_test,
    --cache-show through cacheshow. All three reach sessionfinish with an empty temp dir and
    `collectonly` False. Measured before the fix: each left tests="0"."""
    report, before = _seed_real_report(pytester)

    result = pytester.runpytest_subprocess("--report-xml=junittests.xml", mode)

    assert result.ret == 0, "%s failed outright: %s" % (mode, result.outlines[-5:])
    assert _tests_attr(report) == 3, (
        "%s overwrote the report (tests=%d)" % (mode, _tests_attr(report)))
    assert report.read_bytes() == before


@pytest.mark.parametrize("mode", ["--setup-plan", "--setup-only"])
def test_dry_run_modes_do_not_fabricate_a_green_report(pytester, mode):
    """The worst of the family. --setup-plan/--setup-only run setup and teardown WITHOUT the call
    phase, so the plugin saw passing SETUP reports and recorded test_resolution="Passed".

    The suite here fails outright. Before the fix, a dry run that executed no test body published a
    fully green report for it — worse than tests="0", which at least looks wrong.
    """
    pytester.makepyfile(test_suite="""
        def test_alpha(): assert False
        def test_beta(): assert False
    """)
    report = pytester.path / "junittests.xml"

    result = pytester.runpytest_subprocess("--report-xml=junittests.xml", mode)

    assert result.ret == 0, "%s failed outright: %s" % (mode, result.outlines[-5:])
    assert not report.exists(), (
        "%s published a report for a suite it never executed: %s"
        % (mode, report.read_text()))


def test_a_real_run_that_collects_nothing_still_reports_zero(pytester):
    """The deliberate exception, and the reason the guard is not simply 'never write an empty
    report'. A real session that ran the loop and genuinely collected nothing must overwrite: the
    alternative is leaving the PREVIOUS build's results in place, where they get read as this
    build's. A stale green is far worse than an honest zero."""
    report, _ = _seed_real_report(pytester)

    result = pytester.runpytest_subprocess(
        "--report-xml=junittests.xml", "-k", "ZZZ_matches_nothing")

    assert result.ret == 5, "expected exit 5 (no tests collected), got %s" % result.ret
    assert _tests_attr(report) == 0, (
        "a real 0-test run left the previous build's results in place — they will be reported "
        "as this build's")


def test_a_real_xdist_run_still_writes_the_report(pytester):
    """Guards `tryfirst=True` on our pytest_runtestloop, which is load-bearing ONLY under xdist —
    and therefore invisible to every other test here.

    pytest_runtestloop is firstresult=True: the first implementation that returns a value ends the
    call and everything registered after it is skipped. In a plain run pytest's own implementation
    is registered early, so it runs LAST and ours gets in ahead of it either way. Under -n the
    controller's loop is xdist's DSession.pytest_runtestloop, registered at runtime — without
    tryfirst ours never runs there, `_runtestloop_entered` stays False, and the merge is skipped, so
    an xdist build produces NO report at all. That is a worse failure than the clobbering this guard
    was added to prevent.
    """
    pytest.importorskip(
        "xdist", reason="pytest-xdist is not installed; the tryfirst guard is UNTESTED here")
    pytester.makepyfile(test_suite=SUITE)

    result = pytester.runpytest_subprocess("--report-xml=junittests.xml", "-n", "2")

    result.assert_outcomes(passed=3)
    report = pytester.path / "junittests.xml"
    assert report.exists(), "an xdist run produced no report at all"
    assert _tests_attr(report) == 3


def test_a_skipped_merge_does_not_leave_the_temp_dir_behind(pytester):
    """_merge_temp_dir_into_final() removes the temp dir as its LAST statement, so skipping the
    merge also skipped the cleanup. Self-healing when the report name is fixed (the next run's
    sessionstart rmtree's it) — but a job whose report name varies per build leaks one directory
    per build that nothing ever collects."""
    pytester.makepyfile(test_suite=SUITE)

    result = pytester.runpytest_subprocess("--report-xml=junittests.xml", "--collect-only")

    assert result.ret == 0
    leaked = [p.name for p in pytester.path.iterdir()
              if p.is_dir() and p.name.endswith("_temp")]
    assert not leaked, "the skipped merge left %s behind" % leaked


def test_collect_only_with_no_prior_report_writes_nothing(pytester):
    """A discovery run on a clean workspace must not fabricate an empty report either — an
    artefact collector downstream would pick up a tests="0" file and report a green build with no
    tests."""
    pytester.makepyfile(test_suite=SUITE)
    report = pytester.path / "junittests.xml"
    assert not report.exists()

    result = pytester.runpytest_subprocess("--report-xml=junittests.xml", "--collect-only")

    # Non-vacuity: 'the file is absent' is only meaningful if the run actually RAN. Without this
    # the assertion below passes for any crash, which is exactly how it first went green here.
    assert result.ret == 0, "collect-only run failed: %s" % result.outlines[-8:]
    assert not report.exists(), "a collect-only run created an empty report"


def test_a_second_real_run_still_overwrites(pytester):
    """Guard the guard: the fix must be scoped to collect-only and must NOT make the report
    immutable. A normal re-run has to replace the previous results."""
    pytester.makepyfile(test_suite=SUITE)
    report = pytester.path / "junittests.xml"

    pytester.runpytest_subprocess("--report-xml=junittests.xml")
    assert _tests_attr(report) == 3

    pytester.makepyfile(test_suite=SUITE + "\n    def test_added(): assert True\n")
    pytester.runpytest_subprocess("--report-xml=junittests.xml")

    assert _tests_attr(report) == 4, "a normal re-run no longer refreshes the report"
