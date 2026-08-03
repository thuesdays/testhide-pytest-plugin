# -*- coding: utf-8 -*-
"""A JIRA that cannot be reached must cost a warning, not the run.

Reported from prod (chub_qa):

    INTERNALERROR> File ".../testhide_plugin/plugin.py", line 109, in _init_jira_helper
    INTERNALERROR>   self.config.warn('JIRA_CONNECTION_ERROR', ...)
    INTERNALERROR> AttributeError: 'Config' object has no attribute 'warn'

`Config.warn(code, message)` was deprecated in pytest 3.8 and REMOVED in 4.0; the plugin declares
`pytest>=7`, so all four call sites were dead code that raised on contact. Every one of them sat in
an `except` handler, which is what made it expensive: the handler could only be reached once
something had already gone wrong, and it then threw on top. Raised from `pytest_sessionstart`, that
escapes as INTERNALERROR and the whole session dies -- so the unreachable JIRA this code was written
to shrug off took the build with it, and the traceback named the reporting line rather than the
connection.

The first test is the reported case end to end. The others pin the two decisions the fix rests on.
"""
from __future__ import annotations

import warnings

import pytest

from testhide_plugin.plugin import TestHideWarning, _warn


SUITE = """
    def test_one(): assert True
    def test_two(): assert True
"""

# Fails at URL validation, before any socket is opened, so the run does not wait on a network
# timeout. The retry loop still sleeps 3s between its three attempts, which is why this test takes
# ~10s and the cheap assertions live in their own tests below.
UNREACHABLE = "::not-a-url::"


def test_an_unreachable_jira_does_not_internal_error(pytester):
    """The reported crash. Both halves matter: no INTERNALERROR, and the tests actually ran.

    Asserting only the absence of the string would pass for a session that died some other way.
    """
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest_subprocess(
        "--report-xml=junittests.xml",
        "--jira-url=%s" % UNREACHABLE,
        "--jira-username=someone",
        "--jira-password=secret",
    )

    assert "INTERNALERROR" not in result.stdout.str()
    assert "'Config' object has no attribute" not in result.stdout.str()
    result.assert_outcomes(passed=2)


def test_the_failure_is_reported_rather_than_swallowed(pytester):
    """...and it is not silent either.

    Without this, `except Exception: pass` would pass the test above. The whole point of the
    original code was to SAY that JIRA is not working; a fix that made the run survive by hiding
    the reason would be a different defect wearing the same green tick.
    """
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest_subprocess(
        "--report-xml=junittests.xml",
        "--jira-url=%s" % UNREACHABLE,
        "--jira-username=someone",
        "--jira-password=secret",
    )

    # Both streams: pytest renders warnings in the summary on stdout, but an unhandled one raised
    # during sessionstart is printed by the warnings machinery on stderr. Which of the two carries
    # it is not the behaviour under test -- that it is said at all, is.
    said = result.stdout.str() + result.stderr.str()
    assert "JIRA_CONNECTION_FAILED" in said


def test_warn_cannot_raise_even_under_filterwarnings_error():
    """`warnings.warn` is the documented replacement for `Config.warn`, and on its own it would
    rebuild the same trap: under `filterwarnings = error` it RAISES, from inside the same `except`
    handlers. A suite that turns warnings into errors is common and entirely reasonable; it must not
    turn an unreachable JIRA back into a dead session."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _warn('SOME_CODE', 'some message')  # must not raise


def test_warn_emits_under_an_ordinary_filter():
    """Non-vacuity for the test above: with the escalation removed, a warning IS produced. Pinned
    to the category as well as the text, since that category is what a suite would name to silence
    or escalate these deliberately."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _warn('SOME_CODE', 'some message')

    assert len(caught) == 1
    assert issubclass(caught[0].category, TestHideWarning)
    assert '[testhide:SOME_CODE] some message' == str(caught[0].message)


def test_the_removed_pytest_api_is_gone_from_the_plugin():
    """Negative, and over the source, because that is the shape that fails safe here: a positive
    assertion that `_warn` is called would be satisfied by one call site while three others still
    carried the dead API. There were four."""
    import inspect
    import testhide_plugin.plugin as mod

    src = inspect.getsource(mod)
    assert 'config.warn(' not in src, 'a call to the pytest API removed in 4.0 is back'
    assert not hasattr(pytest.Config, 'warn'), (
        'pytest re-introduced Config.warn -- re-check the rationale in _warn before using it again'
    )
