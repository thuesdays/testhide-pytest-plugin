# -*- coding: utf-8 -*-

__author__ = 'thuesdays@gmail.com'

import os
import signal
import time
import socket
import shutil
import re
from hashlib import md5
import xml.etree.ElementTree as ET
from datetime import datetime

import pluggy
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
    A pytest plugin that provides robust, incremental XML reporting with optional JIRA integration.
    It uses a universal temporary directory and merge strategy for all execution modes,
    ensuring full compatibility with pytest-xdist and pytest-rerunfailures.
    """
    
    def __init__(self, config):
        self.config = config
        self.session = None
        self.report_xml_path = config.option.report_xml
        self.temp_dir = os.path.join(str(config.rootdir), f".{os.path.basename(self.report_xml_path)}_temp")
        self.is_xdist_master = not hasattr(config, "workerinput")
        self.is_xdist_run = config.option.dist != "no"
        self.rerun_counters = {}
        self.test_reports = {}
        
        self.jira_enabled = all([
            config.option.jira_url,
            config.option.jira_username,
            config.option.jira_password
        ])
        self.jira = None
        self._merged = False
        
        if self.is_xdist_master:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, self._signal_flush)
    
    def _init_jira_helper(self):
        """
        Initializes the JIRA connection.
        """
        if not self.jira_enabled:
            return
        
        from jira import JIRA  # Lazy import to avoid dependency if not used
        
        for _ in range(3):  # Retry connection
            try:
                self.jira = JIRA(
                    self.config.option.jira_url,
                    basic_auth=(self.config.option.jira_username, self.config.option.jira_password)
                )
                return  # Success
            except Exception as e:
                self.config.warn('JIRA_CONNECTION_ERROR', f"JIRA connection attempt failed: {e}")
                time.sleep(3)
        
        self.config.warn('JIRA_CONNECTION_FAILED', "Could not establish JIRA connection after multiple retries.")
        self.jira_enabled = False  # Disable if connection failed
    
    def _get_issue_by_test_id(self, test_id: str):
        """
        Gets a JIRA issue by a unique test failure ID.
        """
        if not self.jira:
            return None
        try:
            issues = self.jira.search_issues(f'description ~ "testid#{test_id}" ORDER BY updated')
            return issues[0] if issues else None
        except Exception as e:
            self.config.warn('JIRA_SEARCH_ERROR', f"Failed to search JIRA for testid#{test_id}: {e}")
            return None
    
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
    
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        """
        This wrapper hook intercepts the report after it is created and attaches it to the 'item'
        object itself. This is more reliable than storing the state in a plugin.
        """
        outcome = yield
        report = outcome.get_result()
        
        if report.when in ('setup', 'call') and getattr(report, 'outcome', '') != 'rerun':
            item._final_report = report
    
    @pytest.hookimpl(trylast=True)
    def pytest_runtest_teardown(self, item):
        """
        Runs last, after all other teardowns.
        Takes the final report attached to 'item' and writes XML.
        """
        report = getattr(item, '_final_report', None)
        if not report:
            return
        
        filepath, line, name = report.location
        classname_path = report.nodeid.split('::')
        if len(classname_path) > 2:
            classname = ".".join(classname_path[:-1]).replace('/', '.')
        else:
            classname = os.path.splitext(os.path.basename(filepath))[0]
        
        testcase_attrs = {
            'classname': classname, 'name': name, 'file': str(filepath),
            'line': str(line), 'time': f"{report.duration:.3f}"
        }
        testcase = ET.Element('testcase', **testcase_attrs)
        
        if report.failed:
            tag = 'error' if report.when == 'setup' else 'failure'
            failure_message = str(report.longrepr.reprcrash.message) if hasattr(report.longrepr, 'reprcrash') else str(
                report.longrepr)
            
            if self.jira_enabled:
                try:
                    sub = re.sub(r'\[.+\]$', '', name)
                    fail_id_str = f"{classname}.{sub}.{failure_message}"
                    fail_id = md5(fail_id_str.encode('utf-8')).hexdigest()
                    
                    issue = self._get_issue_by_test_id(fail_id)
                    if issue:
                        issue_text = issue.fields.summary;
                        issue_type = issue.fields.issuetype.name;
                        issue_id = issue.permalink()
                        status_name = issue.fields.status.name;
                        test_resolution = 'Known issue'
                        if status_name in ('Verified', 'Closed'):
                            test_resolution = 'Need to reopen'
                        elif status_name in ('Resolved', 'In Testing'):
                            test_resolution = 'Resolved in branch'
                        failure_message = f'{test_resolution} {issue_id} {issue_type} [{issue_text}]'
                    else:
                        failure_message += f"@@testid#{fail_id}"
                except Exception as e:
                    self.config.warn('JIRA_MARKER_ERROR', f"JIRA marker failed: {e}")
            
            failure_element = ET.SubElement(testcase, tag, message=failure_message)
            failure_element.text = self._get_cleaned_traceback(report)
        
        elif report.skipped:
            skipped_attrs = {'type': 'pytest.skip', 'message': report.longrepr[2]}
            ET.SubElement(testcase, 'skipped',
                          **skipped_attrs).text = f"{report.longrepr[0]}:{report.longrepr[1]}: {report.longrepr[2]}"
        
        all_properties = self.config.hook.pytest_testhide_get_test_case_properties(item=item, report=report)
        flat_properties = [prop for sublist in all_properties for prop in sublist]
        if flat_properties:
            properties_element = ET.SubElement(testcase, 'properties')
            for prop_name, prop_value in flat_properties:
                ET.SubElement(properties_element, 'property', name=str(prop_name), value=str(prop_value))
        
        safe_nodeid = re.sub(r'[^A-Za-z0-9_.\[\]-]', '_', item.nodeid)
        worker = getattr(self.config, 'workerinput', {}).get('workerid', 'master')
        fname = os.path.join(self.temp_dir, f"{safe_nodeid}_{worker}.xml")
        
        ET.ElementTree(testcase).write(fname, encoding='utf-8', xml_declaration=True)
    
    def pytest_sessionstart(self, session):
        """
        Initializes the reporting process. Only the master node will
        clean up the temporary directory and initialize the JIRA connection.
        """
        self.session = session
        if self.is_xdist_master:
            self._init_jira_helper()
            
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
            os.makedirs(self.temp_dir, exist_ok=True)
    
    def _merge_temp_dir_into_final(self):
        """Read everything from temp_dir and update the final junittests.xml (once!)."""
        if self._merged:
            return
        self._merged = True
        
        with FileLock(self.report_xml_path + ".lock"):
            root = ET.Element('testsuites')
            suite = ET.SubElement(root, 'testsuite',
                                  name='pytest',
                                  timestamp=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
                                  hostname=socket.gethostname())
            
            props = ET.SubElement(suite, 'properties')
            ET.SubElement(props, 'property', name='ip_address',
                          value=socket.gethostbyname(socket.gethostname()))
            ET.SubElement(props, 'property', name='hostname',
                          value=socket.gethostname())
            
            for meta in self.config.hook.pytest_testhide_add_metadata(plugin=self):
                for k, v in meta:
                    ET.SubElement(props, 'property', name=str(k), value=str(v))
            
            if os.path.exists(self.temp_dir):
                for fname in sorted(os.listdir(self.temp_dir)):
                    if fname.endswith('.xml'):
                        try:
                            case_tree = ET.parse(os.path.join(self.temp_dir, fname))
                            suite.append(case_tree.getroot())
                        except ET.ParseError:
                            continue
            
            cases = suite.findall('testcase')
            suite.set('tests', str(len(cases)))
            suite.set('failures', str(len(suite.findall('.//failure'))))
            suite.set('errors', str(len(suite.findall('.//error'))))
            suite.set('skipped', str(len(suite.findall('.//skipped'))))
            suite.set('time', f"{sum(float(c.get('time', 0)) for c in cases):.3f}")
            
            tmp = self.report_xml_path + '.tmp'
            ET.indent(ET.ElementTree(root), space='\t', level=0)
            ET.ElementTree(root).write(tmp, encoding='utf-8', xml_declaration=True)
            os.replace(tmp, self.report_xml_path)
        
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def _signal_flush(self, signum, frame):
        try:
            self._merge_temp_dir_into_final()
        finally:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
    
    def pytest_sessionfinish(self, session):
        """
        Finalizes the report. Only the master node will merge all temporary files.
        The entire merge and write process is protected by a file lock.
        """
        if self.is_xdist_master:
            self._merge_temp_dir_into_final()
    
    def pytest_unconfigure(self, config):
        """
        Called by PyTest once at the very end, after pytest_sessionfinish.
        """
        pass


# --- Global functions that pytest discovers via entry points ---

def pytest_addoption(parser):
    """Adds all command-line options for the plugin."""
    group = parser.getgroup('testhide-reporting', 'Testhide Incremental Reporting')
    group.addoption('--report-xml', action='store', default=None, help='Enable incremental XML reporting.')
    
    # JIRA options for automatic integration
    group.addoption('--jira-url', dest='jira_url', default=None, action='store', help='JIRA URL for integration.')
    group.addoption('--jira-username', dest='jira_username', default=None, action='store', help='JIRA username.')
    group.addoption('--jira-password', dest='jira_password', default=None, action='store',
                    help='JIRA password or API token.')


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    try:
        config.pluginmanager.add_hookspecs(hookspecs)
    except pluggy.PluginValidationError:
        pass
    
    if not config.option.report_xml:
        return
    
    base_name = "testhide_plugin"
    
    if config.pluginmanager.has_plugin(base_name + "_active"):
        return
    
    config.pluginmanager.register(TesthidePlugin(config), base_name + "_active")
