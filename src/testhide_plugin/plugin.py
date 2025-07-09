# -*- coding: utf-8 -*-

__author__ = 'thuesdays@gmail.com'

import socket
import xml.etree.ElementTree as ET
from datetime import datetime

import pytest

from . import hookspecs

plugin_instance = None


class TesthidePlugin:
    """
    A pytest plugin to create an incremental XML report with a structure
    matching the provided example.
    """
    
    def __init__(self, config):
        self.config = config
        self.report_xml_path = config.option.report_xml
        self.xml_report_root = None
        self.xml_main_testsuite = None
        self.test_reports = {}
    
    def _write_xml_report(self):
        """Writes the current XML tree to the report file."""
        if self.xml_report_root is None or not self.report_xml_path: return
        try:
            tree = ET.ElementTree(self.xml_report_root)
            ET.indent(tree, space="\t", level=0)  # Using tabs to match example
            tree.write(self.report_xml_path, encoding='utf-8', xml_declaration=True)
        except Exception as e:
            self.config.warn('TESHIDE_PLUGIN_ERROR', f"TesthidePlugin failed to write XML report: {e}")
    
    # --- Pytest Hooks Implementation ---
    
    def pytest_sessionstart(self, session):
        """
        Initializes the report, collects session metadata via hooks,
        and writes the initial report file immediately.
        """
        self.xml_report_root = ET.Element('testsuites')
        self.xml_main_testsuite = ET.SubElement(
            self.xml_report_root, 'testsuite',
            name='pytest',
            timestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
            hostname=socket.gethostname()
        )
        
        properties_element = ET.SubElement(self.xml_main_testsuite, 'properties')
        ET.SubElement(properties_element, 'property', name='ip_address',
                      value=socket.gethostbyname(socket.gethostname()))
        ET.SubElement(properties_element, 'property', name='hostname', value=socket.gethostname())
        
        all_metadata_lists = self.config.hook.pytest_testhide_add_metadata(plugin=self)
        for metadata_list in all_metadata_lists:
            for name, value in metadata_list:
                ET.SubElement(properties_element, 'property', name=str(name), value=str(value))
        
        self._write_xml_report()
    
    def pytest_runtest_logreport(self, report):
        """Captures the test result object."""
        if report.when == 'call' or (report.when == 'setup' and report.failed):
            self.test_reports[report.nodeid] = report
    
    def pytest_runtest_teardown(self, item):
        """Adds the completed test case to the XML report."""
        if item.nodeid not in self.test_reports: return
        report = self.test_reports[item.nodeid]
        
        # Get all necessary attributes for <testcase>
        classname = f"{item.module.__name__}.{item.cls.__name__}" if item.cls else item.module.__name__
        filepath = item.location[0]
        line = str(item.location[1])
        
        testcase_attrs = {
            'classname': classname,
            'name': item.name,
            'file': filepath,
            'line': line,
            'time': f"{report.duration:.3f}"
        }
        testcase = ET.Element('testcase', **testcase_attrs)
        
        # Add failure/skipped tags if necessary
        if report.failed:
            tag = 'error' if report.when == 'setup' else 'failure'
            failure_element = ET.SubElement(testcase, tag, message=str(report.longrepr.reprcrash))
            failure_element.text = str(report.longrepr)
        elif report.skipped:
            skipped_attrs = {'type': 'pytest.skip', 'message': report.longrepr[2]}
            ET.SubElement(testcase, 'skipped',
                          **skipped_attrs).text = f"{report.longrepr[0]}:{report.longrepr[1]}: {report.longrepr[2]}"
        
        # --- Call custom hook to get per-test properties (docstr, info, attachments) ---
        all_properties = self.config.hook.pytest_testhide_get_test_case_properties(item=item, report=report)
        flat_properties = [prop for sublist in all_properties for prop in sublist]
        
        if flat_properties:
            properties_element = ET.SubElement(testcase, 'properties')
            for name, value in flat_properties:
                ET.SubElement(properties_element, 'property', name=name).text = str(value)
        
        self.xml_main_testsuite.append(testcase)
        # To ensure data is saved if the run is aborted, we write after each test.
        # This has a performance cost. For very large test suites, consider
        # moving this call to pytest_sessionfinish only.
        self._update_suite_summary_and_write()
    
    def _update_suite_summary_and_write(self):
        """Updates the main testsuite summary attributes and writes the file."""
        if self.xml_main_testsuite is None: return
        
        testcases = self.xml_main_testsuite.findall('testcase')
        self.xml_main_testsuite.set('tests', str(len(testcases)))
        self.xml_main_testsuite.set('failures', str(len(self.xml_main_testsuite.findall('.//failure'))))
        self.xml_main_testsuite.set('errors', str(len(self.xml_main_testsuite.findall('.//error'))))
        self.xml_main_testsuite.set('skipped', str(len(self.xml_main_testsuite.findall('.//skipped'))))
        total_time = sum(float(tc.get('time', 0)) for tc in testcases)
        self.xml_main_testsuite.set('time', f"{total_time:.3f}")
        
        self._write_xml_report()
    
    def pytest_sessionfinish(self, session):
        """Adds final session properties and performs the final write."""
        # Perform the final write
        self._update_suite_summary_and_write()


# --- Functions that register the plugin and its hooks with pytest ---
def pytest_addoption(parser):
    group = parser.getgroup('testhide-reporting', 'Testhide Incremental Reporting')
    group.addoption('--report-xml', action='store', default=None,
                    help='Enable incremental XML reporting to the specified file.')


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    global plugin_instance
    config.pluginmanager.add_hookspecs(hookspecs)
    if config.option.report_xml:
        plugin_instance = TesthidePlugin(config)


def pytest_unconfigure(config):
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


def pytest_sessionfinish(session):
    if plugin_instance:
        plugin_instance.pytest_sessionfinish(session)
