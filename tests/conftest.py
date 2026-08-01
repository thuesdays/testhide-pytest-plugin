# -*- coding: utf-8 -*-
"""Test harness for testhide-pytest-plugin.

This repository had NO tests before D-0801. The plugin ships to every test node in the farm and
decides whether a report is written at all, so "it looked right in review" is not an adequate
standard for it.

`pytester` runs a real pytest in a throwaway directory, which is the only honest way to test a
plugin: the thing under test is the interaction with pytest's own hook ordering and option
handling, and a unit test that calls the hook directly would not exercise either.
"""
pytest_plugins = ["pytester"]
