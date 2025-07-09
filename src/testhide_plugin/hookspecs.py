# -*- coding: utf-8 -*-

__author__ = 'thuesdays@gmail.com'

from pytest import hookspec


@hookspec
def pytest_testhide_add_metadata(plugin):
    """
    Hook for adding session-level metadata properties to the report.
    Should return a list of (name, value) tuples.
    """


@hookspec
def pytest_testhide_get_test_case_properties(item, report) -> list:
    """
    Hook for getting per-test-case properties (docstr, info, attachments).
    Should return a list of (name, value) tuples.
    """
