# -*- coding: utf-8 -*-
"""The SHAPE of junittests.xml, pinned.

Nothing pinned it before. The only mention of `test_resolution` in this suite was a sentence in
another test's docstring -- prose, not an assertion -- so every attribute and property the backend
reads could be renamed or dropped and the suite would stay green. `pytest_runtest_logreport` was
restructured between 0.2.22 and 0.3.x and that is exactly the code that builds these elements.

Written as SET comparisons on purpose. `assert 'fail_id' in attrs` passes while four siblings go
missing; `assert set(attrs) == {...}` fails the moment the shape moves in either direction -- a
field lost OR a field added without anyone deciding to add it.

Behavioural: a real pytest run through `pytester` produces a real report, and the report is parsed.
The alternative -- asserting over plugin source -- would pass on code that never executes.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest


SUITE = '''
    import pytest

    class TestThings:
        def test_passes(self):
            """A docstring, so the docstr property has something to carry."""
            assert True

        def test_fails(self):
            assert 1 == 2

        @pytest.mark.skip(reason="not today")
        def test_skips(self):
            assert True

        @pytest.mark.xfail(reason="known to be broken")
        def test_xfails(self):
            assert 1 == 2
'''

# Not a wish-list -- this is what the CONSUMERS read, taken from them rather than from the plugin:
#
#   backend  app/ws_handler/handlers/test_provider.py -> classname, name, file, line, time,
#            fail_id, test_resolution, plus @name/@value on <property> and @type/@message on
#            <failure>/<skipped>/<error>
#   client   testhide_client -> classname, name, time, timestamp, tests, line, test_resolution,
#            and the elements <failure> <skipped> <error> <properties> <system-out>
#
# Pinned here so the plugin cannot stop emitting one of them without a red test. Both branches of
# the run build their own attribute dict -- the passing one and the failing one -- and dicts that
# are written twice drift.
TESTCASE_ATTRS = {'classname', 'name', 'file', 'line', 'time', 'fail_id', 'test_resolution'}


def _report(pytester, *extra):
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest_subprocess("--report-xml=junittests.xml", *extra)
    path = pytester.path / "junittests.xml"
    assert path.exists(), "the plugin wrote no report at all:\n%s" % result.stdout.str()
    root = ET.parse(str(path)).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None
    return suite, {c.get("name", "").split("[")[0]: c for c in suite.findall(".//testcase")}


def test_testsuite_carries_its_documented_attributes(pytester):
    suite, _ = _report(pytester)
    for attr in ("name", "timestamp", "tests", "failures", "errors", "skipped", "time"):
        assert suite.get(attr) is not None, "<testsuite> lost @%s" % attr
    # Report Format v1 §4: UTC, ISO-8601, WITH the zone marker. A naive stamp reads as local time
    # to every consumer downstream and silently shifts every report by the node's offset.
    assert suite.get("timestamp", "").endswith("Z"), suite.get("timestamp")


def test_every_testcase_carries_the_full_attribute_set(pytester):
    _, cases = _report(pytester)
    assert set(cases) == {"test_passes", "test_fails", "test_skips", "test_xfails"}, sorted(cases)
    for name, case in cases.items():
        assert set(case.attrib) == TESTCASE_ATTRS, (
            "%s: attribute set moved: missing=%s unexpected=%s"
            % (name, sorted(TESTCASE_ATTRS - set(case.attrib)),
               sorted(set(case.attrib) - TESTCASE_ATTRS)))


def test_resolution_is_written_for_every_outcome(pytester):
    """The field the report page renders. Without a JIRA connection the failing case must still
    say something -- 'Unresolved' -- rather than be absent or empty."""
    _, cases = _report(pytester)
    assert cases["test_passes"].get("test_resolution") == "Passed"
    assert cases["test_fails"].get("test_resolution") == "Unresolved"
    assert cases["test_skips"].get("test_resolution") == "Skipped"
    # xfail is deliberately NOT a skip here: the plugin promotes it to a failure carrying the
    # reason, because an expected failure is a known issue by definition.
    assert cases["test_xfails"].get("test_resolution") == "Known Issue"


def test_fail_id_is_present_and_only_on_failures(pytester):
    """`fail_id` is what the backend keys a defect on. Empty on a pass, non-empty on a failure --
    a fail_id that leaked onto passing rows would fabricate defects."""
    _, cases = _report(pytester)
    assert cases["test_passes"].get("fail_id") == ""
    assert cases["test_fails"].get("fail_id"), "a failure with no fail_id cannot be tracked"


def test_the_failure_element_carries_message_and_traceback(pytester):
    _, cases = _report(pytester)
    failure = cases["test_fails"].find("failure")
    assert failure is not None, "<failure> missing"
    assert failure.get("message"), "<failure> lost @message"
    assert (failure.text or "").strip(), "<failure> lost its traceback text"


def test_the_skipped_element_carries_type_and_message(pytester):
    _, cases = _report(pytester)
    skipped = cases["test_skips"].find("skipped")
    assert skipped is not None, "<skipped> missing"
    assert skipped.get("type"), "<skipped> lost @type"
    assert "not today" in (skipped.get("message") or ""), skipped.get("message")


def test_properties_survive_on_the_testcase(pytester):
    """`docstr`, `info` and `attachment` are contributed through the
    pytest_testhide_get_test_case_properties hook. A suite that supplies none is indistinguishable
    from a plugin that dropped the hook -- so one is supplied here."""
    pytester.makeconftest('''
        def pytest_testhide_get_test_case_properties(item, report):
            return [("docstr", item.function.__doc__ or ""), ("info", "step 1"),
                    ("attachment", "http://example/a.png")]
    ''')
    _, cases = _report(pytester)
    props = cases["test_passes"].find("properties")
    assert props is not None, "<properties> missing -- the hook is no longer consulted"
    names = [p.get("name") for p in props.findall("property")]
    assert {"docstr", "info", "attachment"} <= set(names), names


def test_a_collection_error_is_reported_as_a_testcase(pytester):
    """An import that explodes must appear IN the report. Reported as nothing, a broken file looks
    like a file with no tests, and the run goes green with the suite half-missing."""
    pytester.makepyfile(test_broken="import nonexistent_module_xyz\n")
    result = pytester.runpytest_subprocess("--report-xml=junittests.xml")
    path = pytester.path / "junittests.xml"
    assert path.exists(), result.stdout.str()
    root = ET.parse(str(path)).getroot()
    errors = root.findall(".//testcase/error")
    assert errors, "a collection error produced no <error> row"
    assert errors[0].get("message"), "<error> lost @message"
