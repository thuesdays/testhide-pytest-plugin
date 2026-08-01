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
    because the TPS discovery pass depends on it."""
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest_subprocess(
        "--report-xml=junittests.xml", "--collect-only", "-q")

    assert result.ret == 0
    collected = [ln.strip() for ln in result.outlines if "::" in ln and not ln.startswith("<")]
    assert any("TestAlpha::test_one" in c for c in collected), (
        "collect-only produced no nodeids: %s" % result.outlines[:15])
    assert len(collected) >= 3


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
