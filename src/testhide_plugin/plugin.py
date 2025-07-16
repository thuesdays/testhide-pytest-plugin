# -*- coding: utf-8 -*-

__author__ = 'thuesdays@gmail.com'

import os
import sys
import time
import socket
import xml.etree.ElementTree as ET
from datetime import datetime

import pytest
from . import hookspecs

# This global instance holds our active plugin, but only if it's enabled.
plugin_instance = None


class FileLock:
    """
    A simple context manager for file locking. This prevents race conditions
    when multiple processes or fast-running tests try to write to the same file.
    It now includes a timeout to prevent infinite loops.
    """
    
    def __init__(self, lock_file_path, timeout=15):
        self.lock_file_path = lock_file_path
        self.timeout = timeout
        self._lock_file_handle = None
    
    def __enter__(self):
        """Acquires the lock, waiting up to the specified timeout."""
        start_time = time.time()
        while True:
            try:
                # The flags O_CREAT | O_EXCL ensure that this operation is atomic.
                self._lock_file_handle = os.open(self.lock_file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break  # Lock acquired
            except FileExistsError:
                if time.time() - start_time > self.timeout:
                    raise TimeoutError(
                        f"Could not acquire lock on {self.lock_file_path} within {self.timeout} seconds.")
                time.sleep(0.1)  # Wait for the other process to release the lock
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Releases the lock."""
        if self._lock_file_handle is not None:
            os.close(self._lock_file_handle)
            os.remove(self.lock_file_path)


class TesthidePlugin:
    """
    A pytest plugin that reliably and incrementally updates an XML report
    after each test, using file locks and atomic writes. It also provides
    cleaned, relevant stack traces for failures.
    """
    
    def __init__(self, config):
        self.config = config
        self.report_xml_path = config.option.report_xml
        self.lock_path = self.report_xml_path + ".lock"
        self.test_reports = {}
    
    def _get_cleaned_traceback(self, report):
        """
        Filters the traceback to show only relevant entries by removing
        internal calls from pytest and pluggy.
        """
        if not hasattr(report.longrepr, 'reprtraceback'):
            return str(report.longrepr)
        
        final_trace_lines = []
        blacklist_keywords = ['/_pytest/', '/pluggy/']
        
        for entry in report.longrepr.reprtraceback.reprentries:
            lines_to_process = []
            if hasattr(entry, 'lines'):
                lines_to_process = entry.lines
            elif hasattr(entry, 'longrepr'):
                lines_to_process = str(entry.longrepr).split('\n')
            
            for line in lines_to_process:
                normalized_line = line.replace('\\', '/')
                is_blacklisted = any(keyword in normalized_line for keyword in blacklist_keywords)
                if not is_blacklisted:
                    final_trace_lines.append(line)
        
        summary = report.longrepr.reprcrash.message
        
        if final_trace_lines:
            if not final_trace_lines[0].strip().startswith("Traceback"):
                final_trace_lines.insert(0, "Traceback (most recent call last):")

            if summary not in "".join(final_trace_lines):
                final_trace_lines.append(summary)
            
            return "\n".join(final_trace_lines)
        else:
            return summary


    def pytest_runtest_logreport(self, report):
            """
            Captures the test report for each phase and stores it in a dictionary,
            keyed by the test's unique nodeid.
            """
            if report.when == 'call' or (report.when == 'setup' and report.failed):
                self.test_reports[report.nodeid] = report
    
    def pytest_sessionstart(self, session):
        """
        At the beginning of the session, this hook cleans up any old lock files
        and creates a valid, empty XML report file that already contains all
        session-level metadata.
        """
        try:
            os.remove(self.lock_path)
        except FileNotFoundError:
            pass  # It's okay if the file doesn't exist.
        
        root = ET.Element('testsuites')
        main_suite = ET.SubElement(
            root, 'testsuite',
            name='pytest',
            timestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
            hostname=socket.gethostname()
        )
        
        properties_element = ET.SubElement(main_suite, 'properties')
        ET.SubElement(properties_element, 'property', name='ip_address',
                      value=socket.gethostbyname(socket.gethostname()))
        ET.SubElement(properties_element, 'property', name='hostname', value=socket.gethostname())
        
        all_metadata_lists = self.config.hook.pytest_testhide_add_metadata(plugin=self)
        for metadata_list in all_metadata_lists:
            for name, value in metadata_list:
                ET.SubElement(properties_element, 'property', name=str(name), value=str(value))
        
        tree = ET.ElementTree(root)
        ET.indent(tree, space="\t", level=0)
        tree.write(self.report_xml_path, encoding='utf-8', xml_declaration=True)
    
    def pytest_runtest_teardown(self, item):
        """
        Safely reads the main XML file, adds the result of the completed test,
        and atomically writes the updated content back to disk.
        """
        if item.nodeid not in self.test_reports:
            return
        
        report = self.test_reports[item.nodeid]
        
        with FileLock(self.lock_path):
            try:
                tree = ET.parse(self.report_xml_path)
                main_suite = tree.find('testsuite')
                if main_suite is None: raise FileNotFoundError
            except (FileNotFoundError, ET.ParseError):
                self.pytest_sessionstart(item.session)
                tree = ET.parse(self.report_xml_path)
                main_suite = tree.find('testsuite')
            
            classname = f"{item.module.__name__}.{item.cls.__name__}" if item.cls else item.module.__name__
            filepath, line, _ = item.location
            testcase_attrs = {
                'classname': classname, 'name': item.name, 'file': str(filepath),
                'line': str(line), 'time': f"{report.duration:.3f}"
            }
            testcase = ET.Element('testcase', **testcase_attrs)
            
            if report.failed:
                tag = 'error' if report.when == 'setup' else 'failure'
                cleaned_traceback = self._get_cleaned_traceback(report)
                failure_element = ET.SubElement(testcase, tag, message=str(report.longrepr.reprcrash))
                failure_element.text = cleaned_traceback
            elif report.skipped:
                skipped_attrs = {'type': 'pytest.skip', 'message': report.longrepr[2]}
                ET.SubElement(testcase, 'skipped',
                              **skipped_attrs).text = f"{report.longrepr[0]}:{report.longrepr[1]}: {report.longrepr[2]}"
            
            all_properties = self.config.hook.pytest_testhide_get_test_case_properties(item=item, report=report)
            flat_properties = [prop for sublist in all_properties for prop in sublist]
            if flat_properties:
                properties_element = ET.SubElement(testcase, 'properties')
                for name, value in flat_properties:
                    ET.SubElement(properties_element, 'property', name=str(name), value=str(value))
            
            main_suite.append(testcase)
            
            testcases = main_suite.findall('testcase')
            main_suite.set('tests', str(len(testcases)))
            main_suite.set('failures', str(len(main_suite.findall('.//failure'))))
            main_suite.set('errors', str(len(main_suite.findall('.//error'))))
            main_suite.set('skipped', str(len(main_suite.findall('.//skipped'))))
            total_time = sum(float(tc.get('time', 0)) for tc in testcases)
            main_suite.set('time', f"{total_time:.3f}")
            
            temp_file_path = self.report_xml_path + ".tmp"
            ET.indent(tree, space="\t", level=0)
            tree.write(temp_file_path, encoding='utf-8', xml_declaration=True)
            os.replace(temp_file_path, self.report_xml_path)


# --- Global functions that pytest discovers via entry points ---

def pytest_addoption(parser):
    """Adds the --report-xml command-line option."""
    group = parser.getgroup('testhide-reporting', 'Testhide Incremental Reporting')
    group.addoption('--report-xml', action='store', default=None, help='Enable incremental XML reporting.')


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Creates the plugin instance if enabled by the command line option."""
    global plugin_instance
    config.pluginmanager.add_hookspecs(hookspecs)
    if config.option.report_xml:
        plugin_instance = TesthidePlugin(config)


def pytest_unconfigure(config):
    """Cleans up the plugin instance at the end."""
    global plugin_instance
    plugin_instance = None


def pytest_sessionstart(session):
    if plugin_instance:
        plugin_instance.pytest_sessionstart(session)


def pytest_runtest_logreport(report):
    if plugin_instance:
        plugin_instance.pytest_runtest_logreport(report)


def pytest_runtest_teardown(item):
    if plugin_instance:
        plugin_instance.pytest_runtest_teardown(item)