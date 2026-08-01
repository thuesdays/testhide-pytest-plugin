# -*- coding: utf-8 -*-

__author__ = 'thuesdays@gmail.com'

import json
import os
import signal
import time
import socket
import shutil
import re
from hashlib import md5
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pluggy
import pytest
from . import hookspecs
from .report_core import SCHEMA_VERSION, compute_fail_id

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
        self.is_xdist_run = getattr(config.option, 'dist', 'no') != 'no'
        self.rerun_counters = {}
        self.test_reports = {}
        
        self.jira_enabled = all([
            config.option.jira_url,
            config.option.jira_username,
            config.option.jira_password
        ])
        self.jira = None
        self._merged = False
        # D-0801: set by pytest_runtestloop. See _session_executed_tests — the report may only be
        # replaced by a session that actually entered the run loop.
        self._runtestloop_entered = False

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
            issues = self.jira.search_issues(f'description ~ "{test_id}" ORDER BY updated')
            return issues[0] if issues else None
        except Exception as e:
            self.config.warn('JIRA_SEARCH_ERROR', f"Failed to search JIRA for {test_id}: {e}")
            return None
    
    def _log_failure_info(self, nodeid, fail_id, issue=None):
        """
        Logs the fail_id and optional JIRA issue info to console and log.
        """
        parts = [f'fail_id={fail_id}']
        if issue:
            try:
                parts.append(f'jira={issue.key}')
                parts.append(f'status={issue.fields.status.name}')
                parts.append(f'summary={issue.fields.summary}')
            except Exception:
                pass
        info = ' | '.join(parts)
        message = f'[testhide_fail_info][{nodeid}]: {info}'
        print(message)
    
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
            
            return "".join(final_trace_lines)
        else:
            return summary
    
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        """
        This wrapper hook intercepts the report after it is created and attaches it to the 'item'
        object itself. We store per-phase reports so that pytest_runtest_logreport
        can pick the "worst" outcome after all phases (including teardown) complete.
        """
        outcome = yield
        report = outcome.get_result()
        
        if getattr(report, 'outcome', '') != 'rerun':
            # Store reports per-phase on the item
            if not hasattr(item, '_phase_reports'):
                item._phase_reports = {}
            item._phase_reports[report.when] = report
            # Keep _final_report for backward compat (best non-teardown report)
            if report.when in ('setup', 'call'):
                item._final_report = report
        
        if call.when == 'call' and call.excinfo:
            fail_message = str(call.excinfo.value)
            try:
                fail_id = compute_fail_id(
                    item.module.__name__,
                    item.cls.__name__ if item.cls else item.module.__name__,
                    item.name,
                    call.excinfo.typename,
                    fail_message,
                )
            except AttributeError:
                return
            item.fail_id = fail_id
    
    def pytest_runtest_logreport(self, report):
        """
        Called after each phase report (setup, call, teardown) is generated.
        We write XML only after the teardown phase — at that point all three
        phase reports are available, including teardown fixture errors that
        pytest_runtest_teardown would have missed.
        """
        if report.when != 'teardown':
            return
        
        # Retrieve the item via the stored reference in the session
        item = report._item if hasattr(report, '_item') else None
        if item is None:
            # Fallback: look up item from session by nodeid
            try:
                item = self._find_item_by_nodeid(report.nodeid)
            except Exception:
                pass
        if item is None:
            return
        
        phase_reports = getattr(item, '_phase_reports', {})
        setup_report = phase_reports.get('setup')
        call_report = phase_reports.get('call')
        teardown_report = phase_reports.get('teardown')
        
        # Determine the "effective" report to use for the XML entry.
        # Priority: setup error > call failure > teardown error > call passed
        if setup_report and setup_report.failed:
            effective_report = setup_report
        elif call_report and call_report.failed:
            effective_report = call_report
        elif teardown_report and teardown_report.failed:
            effective_report = teardown_report
        elif call_report:
            effective_report = call_report
        elif setup_report:
            effective_report = setup_report
        else:
            effective_report = teardown_report
        
        if not effective_report:
            return
        
        # Calculate total duration across all phases
        total_duration = sum(
            getattr(r, 'duration', 0) for r in phase_reports.values() if r
        )
        
        filepath, line, _ = effective_report.location
        name = item.name
        classname_path = effective_report.nodeid.split('::')
        if len(classname_path) > 2:
            # Strip file extension from the path segment before converting to
            # dotted classname. Without this, nodeid "tests/path/test_file.py::TestClass::test_foo"
            # produces classname "tests.path.test_file.py.TestClass" — the extension segment
            # confuses reconstruction. Works for any language (.py, .ts, .cs, .java, etc.).
            raw_classname = ".".join(classname_path[:-1]).replace('/', '.')
            # Remove known file extensions that appear as dot-separated segments
            _ext_set = {'py', 'ts', 'js', 'jsx', 'tsx', 'cs', 'java', 'kt', 'rb', 'go', 'rs', 'cpp', 'cc', 'c', 'swift', 'scala', 'php'}
            _parts = raw_classname.split('.')
            classname = '.'.join(p for p in _parts if p.lower() not in _ext_set)
        else:
            classname = os.path.splitext(os.path.basename(filepath))[0]
        
        fail_id = getattr(item, 'fail_id', None)
        test_resolution = 'Unresolved'
        
        # Check if teardown failed (even if call passed)
        teardown_failed = teardown_report and teardown_report.failed
        
        # Detect xpass (unexpected pass on a strict xfail marker) BEFORE the
        # failed branch.  pytest marks these as 'failed' but they are actually
        # passing tests whose xfail marker should be removed — treat as Passed.
        is_xpass = (
            effective_report.failed
            and not teardown_failed
            and (
                getattr(effective_report, 'wasxfail', None)
                or (isinstance(effective_report.longrepr, str) and effective_report.longrepr.startswith('[XPASS(strict)]'))
            )
        )
        
        if is_xpass:
            testcase_attrs = {
                'classname': classname, 'name': name, 'file': str(filepath),
                'line': str(line), 'time': f"{total_duration:.3f}", "fail_id": '',
                "test_resolution": "Passed",
            }
            testcase = ET.Element('testcase', **testcase_attrs)
        
        elif effective_report.failed or teardown_failed:
            # Determine which report to use for the error/failure message
            if teardown_failed and not (effective_report.failed and effective_report.when in ('setup', 'call')):
                # Teardown error takes precedence if call didn't fail
                error_report = teardown_report
                tag = 'error'
                test_resolution = 'Teardown Error'
            elif effective_report.when == 'setup':
                error_report = effective_report
                tag = 'error'
            else:
                error_report = effective_report
                tag = 'failure'
            
            failure_message = str(error_report.longrepr.reprcrash.message) if hasattr(error_report.longrepr, 'reprcrash') else str(
                error_report.longrepr)
            
            if self.jira_enabled and tag == 'failure':
                try:
                    if fail_id:
                        issue = self._get_issue_by_test_id(fail_id)
                        if issue:
                            issue_text = issue.fields.summary
                            issue_type = issue.fields.issuetype.name
                            issue_id = issue.permalink()
                            status_name = issue.fields.status.name
                            if status_name in ('Verified', 'Closed'):
                                if hasattr(issue.fields, 'customfield_10020') and issue.fields.customfield_10020:
                                    status = issue.fields.customfield_10020.value
                                    if status_name == 'Verified' and status == 'Verified at Branch':
                                        test_resolution = 'Verified at Branch'
                                    else:
                                        test_resolution = 'Need to reopen'
                                else:
                                    test_resolution = 'Need to reopen'
                            elif status_name in ['Resolved', 'In Testing']:
                                test_resolution = 'Resolved in branch'
                            else:
                                test_resolution = 'Known Issue'
                            failure_message = f'{test_resolution} {issue_id} {issue_type} [{issue_text}]'
                except Exception as e:
                    self.config.warn('JIRA_MARKER_ERROR', f"JIRA marker failed: {e}")
            
            testcase_attrs = {
                'classname': classname, 'name': name, 'file': str(filepath),
                'line': str(line), 'time': f"{total_duration:.3f}", "fail_id": fail_id or '', "test_resolution": test_resolution,
            }
            testcase = ET.Element('testcase', **testcase_attrs)
            failure_element = ET.SubElement(testcase, tag, message=failure_message)
            failure_element.text = self._get_cleaned_traceback(error_report)
        
        elif effective_report.skipped:
            # Detect xfail (expected failure): pytest marks these as "skipped"
            # but sets the wasxfail attribute with the xfail reason.
            xfail_reason = getattr(effective_report, 'wasxfail', None)
            if xfail_reason:
                # xfail tests are reported as failures with "Known Issue" resolution,
                # matching the backend/frontend convention for known bugs.
                test_resolution = 'Known Issue'
                # Extract error message from longrepr
                if isinstance(effective_report.longrepr, tuple) and len(effective_report.longrepr) >= 3:
                    failure_message = f"Known Issue: {xfail_reason}"
                    failure_text = f"{effective_report.longrepr[0]}:{effective_report.longrepr[1]}: {effective_report.longrepr[2]}"
                else:
                    failure_message = f"Known Issue: {xfail_reason}"
                    failure_text = str(effective_report.longrepr)
                testcase_attrs = {
                    'classname': classname, 'name': name, 'file': str(filepath),
                    'line': str(line), 'time': f"{total_duration:.3f}", "fail_id": fail_id or '',
                    "test_resolution": test_resolution,
                }
                testcase = ET.Element('testcase', **testcase_attrs)
                failure_element = ET.SubElement(testcase, 'failure', message=failure_message)
                failure_element.text = failure_text
            else:
                test_resolution = 'Skipped'
                testcase_attrs = {
                    'classname': classname, 'name': name, 'file': str(filepath),
                    'line': str(line), 'time': f"{total_duration:.3f}", "fail_id": '',
                    "test_resolution": test_resolution,
                }
                testcase = ET.Element('testcase', **testcase_attrs)
                # longrepr is a tuple (file, lineno, message) for normal skips,
                # but can be a ReprExceptionInfo object for xfail+teardown-error.
                if isinstance(effective_report.longrepr, tuple) and len(effective_report.longrepr) >= 3:
                    skip_message = str(effective_report.longrepr[2])
                    skip_text = f"{effective_report.longrepr[0]}:{effective_report.longrepr[1]}: {skip_message}"
                else:
                    skip_message = str(effective_report.longrepr)
                    skip_text = skip_message
                skipped_attrs = {'type': 'pytest.skip', 'message': skip_message}
                ET.SubElement(testcase, 'skipped', **skipped_attrs).text = skip_text
        else:
            testcase_attrs = {
                'classname': classname, 'name': name, 'file': str(filepath),
                'line': str(line), 'time': f"{total_duration:.3f}", "fail_id": '',
                "test_resolution": "Passed",
            }
            testcase = ET.Element('testcase', **testcase_attrs)
        
        all_properties = self.config.hook.pytest_testhide_get_test_case_properties(item=item, report=effective_report)
        flat_properties = [prop for sublist in all_properties for prop in sublist]
        if flat_properties:
            properties_element = ET.SubElement(testcase, 'properties')
            for prop_name, prop_value in flat_properties:
                ET.SubElement(properties_element, 'property', name=str(prop_name), value=str(prop_value))
        
        # Embed captured test output (steps, HTTP logs) in <system-out>
        try:
            test_output = self.config.hook.pytest_testhide_get_test_output(item=item, report=effective_report)
            if test_output:
                system_out = ET.SubElement(testcase, 'system-out')
                system_out.text = str(test_output)
        except Exception:
            pass  # Hook not implemented or failed — skip silently
                
        nodeid_hash = md5(item.nodeid.encode('utf-8')).hexdigest()
        worker = getattr(self.config, 'workerinput', {}).get('workerid', 'master')
        fname = os.path.join(self.temp_dir, f"{nodeid_hash}_{worker}.xml")
        
        if os.name == 'nt' and not fname.startswith('\\\\?\\'):
            fname = f"\\\\?\\{os.path.abspath(fname)}"
        
        os.makedirs(os.path.dirname(fname), exist_ok=True)
        ET.ElementTree(testcase).write(fname, encoding='utf-8', xml_declaration=True)
    
    def _find_item_by_nodeid(self, nodeid):
        """Look up a test item from the session by its nodeid."""
        if self.session:
            for item in self.session.items:
                if item.nodeid == nodeid:
                    return item
        return None
    
    def pytest_collectreport(self, report):
        """
        Captures collection errors (import failures, conftest crashes, syntax errors)
        and writes them as <testcase> with <error> to the XML.
        Without this hook, ERROR tests from the collection phase are completely
        missing from the report because pytest never creates test items for them,
        so pytest_runtest_teardown is never called.
        """
        if not report.failed:
            return

        nodeid = report.nodeid
        # Derive classname: replace path separators with dots, strip .py
        file_part = nodeid.split("::")[0]
        classname = file_part.replace("/", ".").replace("\\", ".").replace(".py", "")
        name = nodeid.rsplit("::", 1)[-1] if "::" in nodeid else nodeid

        message = (
            str(report.longrepr.reprcrash.message)
            if hasattr(report.longrepr, 'reprcrash')
            else str(report.longrepr)
        )
        trace = str(report.longrepr)

        testcase = ET.Element('testcase',
            classname=classname,
            name=name,
            file=file_part,
            line="0",
            time="0.000",
            fail_id="",
            test_resolution="Collection Error",
        )
        error_el = ET.SubElement(testcase, 'error', message=message)
        error_el.text = trace

        # Write to temp dir using the same pattern as pytest_runtest_teardown
        nodeid_hash = md5(nodeid.encode('utf-8')).hexdigest()
        worker = getattr(self.config, 'workerinput', {}).get('workerid', 'master')
        fname = os.path.join(self.temp_dir, f"{nodeid_hash}_{worker}.xml")

        if os.name == 'nt' and not fname.startswith('\\\\?\\'):
            fname = f"\\\\?\\{os.path.abspath(fname)}"

        os.makedirs(os.path.dirname(fname), exist_ok=True)
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
                                  name=(self.config.getoption('suite_name') or 'pytest'),
                                  # ISO-8601 UTC WITH the trailing 'Z' (Report Format v1 §4).
                                  timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                                  hostname=socket.gethostname())

            props = ET.SubElement(suite, 'properties')
            # REQUIRED in v1 (parity with unittest / .NET / JS plugins).
            ET.SubElement(props, 'property', name='testhide_schema_version', value=SCHEMA_VERSION)
            ET.SubElement(props, 'property', name='ip_address',
                          value=socket.gethostbyname(socket.gethostname()))
            ET.SubElement(props, 'property', name='hostname',
                          value=socket.gethostname())

            # --meta KEY=VALUE (repeatable) → suite properties.
            for _item in (self.config.getoption('meta') or []):
                if '=' in _item:
                    _k, _v = _item.split('=', 1)
                    _k = _k.strip()
                    if _k:
                        ET.SubElement(props, 'property', name=_k, value=_v)

            # Programmatic metadata via the hook (back-compat / conftest plugins).
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
    
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session):
        """Record that this session reached the run loop. Returns None so the real loop still runs.

        tryfirst matters: pytest_runtestloop is firstresult=True, so an implementation that returns
        a value (pytest's own, xdist's DSession) ends the call and everything after it is skipped.
        """
        self._runtestloop_entered = True
        return None

    def _session_executed_tests(self, session) -> bool:
        """Did this session actually run the test protocol?

        Only such a session is allowed to replace the report. Anything else reaches
        pytest_sessionfinish with an empty temp dir and would os.replace() the last real
        junittests.xml with a document containing tests="0".

        The first version of this guard whitelisted --collect-only alone. That was wrong: pytest
        calls wrap_session — and therefore both session hooks — from FOUR places, and three of them
        run a full session that executes nothing:
            _pytest/main.py          _main                    (a real run)
            _pytest/fixtures.py      _showfixtures_main       (--fixtures)
            _pytest/fixtures.py      _show_fixtures_per_test  (--fixtures-per-test)
            _pytest/cacheprovider.py cacheshow                (--cache-show)
        Measured against a workspace holding a real tests="3" report, each of the last three left
        tests="0" — the same data loss, through a door the mode whitelist did not cover, on pytest
        7.4.4 and 9.0.3 alike. Whitelisting a mode is a per-door fix and every new pytest mode
        opens another door, so this asserts the property instead.

        Two independent signals, because neither alone is sufficient:
          - --fixtures / --fixtures-per-test / --cache-show never enter the run loop at all, so the
            flag catches them (and any future listing mode built the same way).
          - --collect-only, --setup-only and --setup-plan DO enter the run loop, so they have to be
            named. --setup-only/--setup-plan matter more than they look: they run the setup and
            teardown phases without `call`, so pytest_runtest_logreport sees passing SETUP reports
            and the plugin records test_resolution="Passed". A dry run that executed no test body
            published a fully green report for a suite whose every test fails. That is worse than
            tests="0", which at least looks wrong.

        A real run that collects zero tests deliberately returns True: it entered the loop and
        genuinely produced no results, so writing tests="0" is the honest outcome. Keeping the
        previous report there would attribute the LAST build's results to this one.
        """
        opt = session.config.option
        for dry_run_flag in ("collectonly", "setuponly", "setupplan"):
            if getattr(opt, dry_run_flag, False):
                return False
        return self._runtestloop_entered

    def _discard_temp_dir(self):
        """Drop the temp dir without merging it.

        _merge_temp_dir_into_final() removes it as its last statement, so skipping the merge also
        skipped the cleanup and every non-executing run left a `.junittests.xml_temp/` behind. The
        next real run's pytest_sessionstart rmtree's it, so it is self-healing for a fixed report
        name — but a job whose report name varies per build (--report-xml=junit_build17.xml) leaves
        one directory per build that nothing ever collects.
        """
        try:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            # Never let cleanup failure change the exit status of a build.
            pass

    def pytest_sessionfinish(self, session):
        """
        Finalizes the report. Only the master node will merge all temporary files.
        The entire merge and write process is protected by a file lock.
        """
        if not self._session_executed_tests(session):
            if self.is_xdist_master:
                self._discard_temp_dir()
            return

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

    # Suite identity + metadata (parity with the unittest / .NET / JS plugins).
    group.addoption('--suite-name', dest='suite_name', default='pytest', action='store',
                    help='Value for <testsuite name="..."> in the report. Default: pytest.')
    group.addoption('--meta', dest='meta', default=[], action='append', metavar='KEY=VALUE',
                    help='Add a suite-level <property name=KEY value=VALUE>. Repeatable, '
                         'e.g. --meta build=1042 --meta branch=main.')

    # JIRA options for automatic integration
    group.addoption('--jira-url', dest='jira_url', default=None, action='store', help='JIRA URL for integration.')
    group.addoption('--jira-username', dest='jira_username', default=None, action='store', help='JIRA username.')
    group.addoption('--jira-password', dest='jira_password', default=None, action='store',
                    help='JIRA password or API token.')
    
    # Quarantine support
    group.addoption(
        '--quarantine-file', dest='quarantine_file', default=None, action='store',
        help='Path to file with quarantined test nodeids (one per line). '
             'Can also be set via TESTHIDE_QUARANTINE_FILE env var.'
    )

    group.addoption(
        '--testhide-session-dir', dest='testhide_session_dir', default=None, action='store',
        help='Directory carrying the wave protocol of a PERSISTENT session: the scheduler feeds '
             'this one pytest process batch after batch instead of starting a process per batch, '
             'so class- and module-scoped fixtures are paid once per executor. Normally delivered '
             'as TESTHIDE_SESSION_DIR, because a job that starts its tests with its own command '
             'has nowhere to put an argument.'
    )

    group.addoption(
        '--testhide-batch', dest='testhide_batch', default=False, action='store_true',
        help='This run executes an explicit set of nodeids chosen by the TestHide scheduler. '
             'Neutralises -m/-k and disables xdist so the given tests actually run, once each.'
    )


# Everything that can drop a scheduled nodeid, and what each one is.
#
# Rewritten after the RA-3 measurement, which found the first version closed three doors to one
# symptom and left at least four more open — all producing the identical outcome: a nodeid the
# scheduler assigned, that neither runs, nor reports, nor fails. Measured, batch of 4 explicit
# nodeids under --testhide-batch:
#
#     --deselect <nodeid>   4 passed, 1 deselected, rc=0      (same as -m, different door)
#     --lf (seeded cache)   1 failed, 3 deselected            (worst: 3 of 4 silently dropped)
#     --sw                  run 2: 1 failed, 1 deselected
#     -x / --maxfail=1      failure on test 3 of 20 -> the junit the backend reads contains
#                           EXACTLY 3 <testcase> elements. The other 17 produce no row at all.
#
# Deliberately NOT neutralised, because measurement showed them inert against explicit nodeids:
# --ignore / --ignore-glob, conftest collect_ignore, testpaths / norecursedirs (all filter
# DIRECTORY arguments), and --ff / --nf (reorder only, nothing is dropped).
_BATCH_OVERRIDES = (
    # attr, neutral value, how to describe it in the log
    ('markexpr', '', '-m %r'),
    ('keyword', '', '-k %r'),
    ('deselect', [], '--deselect %r'),
    ('lf', False, '--lf (%r)'),
    ('stepwise', False, '--sw (%r)'),
    ('stepwise_skip', False, '--sw-skip (%r)'),
    ('maxfail', 0, '--maxfail/-x (%r)'),
    # xdist. Guarded by hasattr below: these do not exist when xdist is not installed.
    ('dist', 'no', '--dist %r'),
    ('numprocesses', 0, '-n %r'),
    ('tx', [], '--tx %r'),
)

_BATCH_REASONS = {
    'markexpr': 'the scheduler already selected these tests; re-filtering silently drops some',
    'keyword': 'the scheduler already selected these tests; re-filtering silently drops some',
    'deselect': 'the scheduler already selected these tests; re-filtering silently drops some',
    'lf': 'a stale last-failed cache would deselect most of the batch',
    'stepwise': 'stepwise truncates the batch at the first failure and deselects the rest',
    'stepwise_skip': 'stepwise truncates the batch at the first failure and deselects the rest',
    'maxfail': 'stopping early leaves every remaining nodeid with no result at all, and the '
               'backend cannot tell "failed" from "never ran"',
    'dist': 'parallel workers re-pay class-scoped fixtures per worker and run several instances of '
            'a single-instance application',
    'numprocesses': 'parallel workers re-pay class-scoped fixtures per worker and run several '
                    'instances of a single-instance application',
    'tx': 'parallel workers re-pay class-scoped fixtures per worker and run several instances of a '
          'single-instance application',
}


def _neutralise_batch_argv(config) -> None:
    """Make an explicitly-scheduled batch run exactly the tests it was given.

    Only fires under --testhide-batch, so an ordinary run is untouched.

    TestHide's distributed strategy hands each executor an explicit list of nodeids, then reuses
    the JOB'S OWN build script to run them — the script is the customer's, so whatever it carries
    comes along. A leftover selection flag DESELECTS tests the executor was explicitly told to run.
    Those tests are not re-queued (the executor reported no result for them, it did not fail them),
    so they sit in the queue until the heartbeat sweep reclaims them and hands them to another
    executor — which deselects them too. The suite finishes with tests permanently unaccounted for.
    See _BATCH_OVERRIDES above for the full list and the measurement behind each entry.

    On xdist specifically: the class-scoped fixture this scheduler exists to amortise is paid once
    PER WORKER, the app under test is a desktop application that allows a single instance per
    machine, and the whole farm shares one Steam account. Measured on a 7-test module: guard off ->
    4 class-fixture setups, guard on -> 1.

    On the two measurements behind the implementation, stated apart because an earlier version of
    this comment ran them together and got the claim wrong:

      * Putting `--dist no` IN THE SCRIPT does not work. xdist's own plugin overrides --dist when
        -n is present, so `-n 4 --dist no` still forks 4 workers — measured, 4 class-fixture setups.
      * Setting `config.option.dist` HERE does work, because pytest_configure runs after that
        override — measured, 4 workers collapse to 1 setup.

    numprocesses and tx are zeroed as well. Measured by mutation, over the suite in
    tests/test_batch_argv_neutralisation.py, so the next reader does not have to take it on trust:

        drop 'dist', keep numprocesses+tx        -> 47 passed   (survives)
        keep 'dist', drop numprocesses+tx        -> 47 passed   (survives)
        drop all three                           -> 8 failed    (killed)

    Either half alone suffices — xdist computes `_is_distribution_mode = dist != "no" and bool(tx)`
    — so neither is load-bearing GIVEN the other, and the two surviving mutants above are equivalent
    rather than a hole in the tests. The pair is not decoration either: `--tx popen --tx popen` with
    no -n reaches distribution mode without setting numprocesses at all.

    NOT `-p no:xdist`: that removes the option definitions, so a script that contains -n dies at
    argument parsing with rc=4 before anything runs. Verified against five hostile argv shapes
    (-n 4, -n auto, --dist loadscope, --dist worksteal, an explicit -p xdist).
    """
    opt = config.option

    for attr, neutral, shape in _BATCH_OVERRIDES:
        if not hasattr(opt, attr):
            continue
        current = getattr(opt, attr)
        if current == neutral or not current:
            continue
        setattr(opt, attr, neutral)
        # Announced per OVERRIDE, not per flag family. RA-3: the old code logged only when
        # `numprocesses` was set, so `--dist each --tx popen --tx popen` silently collapsed two
        # workers into one with no [testhide] line at all — a rewrite of the customer's arguments
        # that left no trace anywhere.
        _batch_log(config, "ignoring %s for this batch: %s" % (
            shape % (current,), _BATCH_REASONS.get(attr, 'it can drop scheduled tests')))


def _batch_log(config, message: str) -> None:
    """Say what was overridden, on the terminal the build captures.

    Silently rewriting a customer's own arguments is the kind of help that costs an afternoon when
    it turns out to be wrong, so every override announces itself in the build log.

    print(), not the terminal reporter. The first version preferred getplugin('terminalreporter')
    and fell back to print — but RA-3 measured that at our tryfirst pytest_configure (and earlier
    still, at pytest_cmdline_main) the reporter has not been registered yet: TerminalReporter
    registers in _pytest.terminal.pytest_configure, a plain hookimpl that runs after ours. The
    branch was unreachable from the only call site, and its docstring described a path that never
    ran. The line lands above "=== test session starts ===", which the build captures all the same.
    """
    print("[testhide] %s" % message)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """Disarm --looponfail before xdist acts on it.

    The one door that CANNOT be closed from pytest_configure. xdist implements looponfail in
    `pytest_cmdline_main` (looponfail.py:41-47), which runs BEFORE any pytest_configure and never
    returns: it enters `while 1` and re-runs failures forever. Measured — `pytest --testhide-batch
    -f <file>` produced no output at all and had to be killed at 20s. On a farm that is an executor
    holding its node until the job timeout, reporting nothing for every nodeid it was given.

    Returning None (not a value) lets the rest of the chain run normally; by the time xdist's own
    impl is called the flag reads False, so it delegates to the ordinary main.
    """
    if not getattr(config.option, 'testhide_batch', False):
        return None
    if getattr(config.option, 'looponfail', False):
        config.option.looponfail = False
        _batch_log(config, "ignoring -f/--looponfail for this batch: it never returns, so the "
                           "executor would hold its node until the job timeout and report nothing")
    return None


# --------------------------------------------------------------------------- persistent session
#
# TestHide's distributed strategy hands an executor one batch at a time. Today each batch is a fresh
# pytest process, so every class- and module-scoped fixture is re-paid per batch — and on this farm
# a class fixture is a Steam login and an application start, measured in seconds against test bodies
# measured in hundredths.
#
# A persistent session runs batch after batch as WAVES inside one pytest process. Measured on a
# 4-class x 5-test suite with a 2s class fixture, 4 nodeids per batch (pytest 9.1.1):
#
#     per-batch, 5 processes (today)      18.1s    5 sessions, 5 modules, 8 class setups
#     waves, one process                   9.3s    1 session,  1 module,  4 class setups
#     the same 20 nodeids in one go        9.3s    <- the ceiling; a wave costs 0.4% over it
#
# so -48.6% against today, and 0.4% off the best any arrangement could do.

_WAVE_POLL_SEC = 0.02
# How long a live session waits for the next wave before giving up. The executor holds a node for
# this long doing nothing if the client dies without writing `stop`, so it is deliberately shorter
# than the build-level timeouts that would otherwise be the only thing to notice.
_WAVE_IDLE_TIMEOUT_SEC = 600.0


class _WaveBoundary:
    """The sentinel handed to pytest as `nextitem` for the LAST item of a wave.

    SetupState.teardown_exact does:

        needed_collectors = (nextitem and nextitem.listchain()) or []

    and then pops the stack until it matches. So this object's chain decides what survives a wave
    boundary, and all three plausible answers were measured:

      * None                  pops EVERYTHING, session included. 5 waves -> 5 session setups: the
                              application restarts once per wave. Zero gain, and it looks like it
                              works.
      * [session]             keeps the session, pops module and class. Measured module 5x, class
                              8x, -4.3% against today. Also looks like it works.
      * the finished item's   pops NOTHING. The stack stands exactly as the last test left it and is
        own chain             trimmed at the START of the next wave, when the destination is known
                              and the pops are the ones a vanilla run would have made anyway.

    Only the third gives fixture parity with vanilla, so the chain is rebound at every boundary
    rather than built once from the session.
    """

    __slots__ = ('_chain',)

    def __init__(self):
        self._chain = []

    def hold(self, item):
        self._chain = list(item.listchain())
        return self

    def listchain(self):
        return self._chain


class _WaveRunner:
    """Runs waves of nodeids inside one session, and reports each wave as it finishes.

    The control channel is a DIRECTORY, named by the TESTHIDE_SESSION_DIR environment variable:

        wave-<n>.json        written by the client:  {"nodeids": [...]}
        wave-<n>.done.json   written by us:          {"results": [...], "not_collected": [...]}
        stop                 written by the client:  end the session

    A directory rather than an argument, because the executor may be started by the CUSTOMER'S OWN
    run_command template, where the client has nowhere to insert a flag; environment variables are
    already threaded into that path. Files rather than a pipe, because a wave has to survive the
    client reading stdout on a different schedule than the tests write to it.
    """

    def __init__(self, control_dir):
        self.control_dir = control_dir
        self._wave_reports = {}

    # -- report capture -------------------------------------------------------------------
    def pytest_runtest_logreport(self, report):
        """Collect the phase reports of the CURRENT wave.

        The junit report is still assembled once, at session finish, exactly as before. This is a
        separate, per-wave answer for the scheduler, which cannot wait for the session to end: a
        nodeid it assigned and never hears about stays `running` until a sweep reclaims it.
        """
        rows = self._wave_reports.setdefault(report.nodeid, {})
        rows[report.when] = report

    def _verdict(self, nodeid):
        phases = self._wave_reports.get(nodeid) or {}
        duration = sum(getattr(r, 'duration', 0.0) or 0.0 for r in phases.values())
        for when in ('setup', 'call', 'teardown'):
            rep = phases.get(when)
            if rep is None:
                continue
            if rep.failed:
                # A failure in setup or teardown is an ERROR in pytest's own vocabulary, and the
                # backend distinguishes them: an error is a broken environment, a failure is a
                # broken test, and conflating them sends the wrong node to triage.
                return ('failed' if when == 'call' else 'error'), duration
            if rep.skipped:
                # Both phases, not setup alone. A `pytest.skip()` inside the test BODY produces a
                # skipped CALL report, and reading only the setup phase reported it as passed —
                # found by the test next to this one. A skip counted as a pass is the one direction
                # that inflates a green build.
                return 'skipped', duration
        return ('passed' if phases else 'missing'), duration

    # -- the loop -------------------------------------------------------------------------
    def _read_wave(self, n):
        path = os.path.join(self.control_dir, 'wave-%d.json' % n)
        stop = os.path.join(self.control_dir, 'stop')
        deadline = time.time() + _WAVE_IDLE_TIMEOUT_SEC
        while True:
            if os.path.exists(path):
                try:
                    with open(path, encoding='utf-8') as fh:
                        payload = json.load(fh)
                except (ValueError, OSError):
                    # Written but not yet flushed. Re-read rather than treating a partial file as
                    # an empty wave, which would report every nodeid in it as not collected.
                    time.sleep(_WAVE_POLL_SEC)
                    continue
                return [str(x) for x in (payload.get('nodeids') or [])]
            if os.path.exists(stop):
                return None
            if time.time() > deadline:
                print('[testhide] no wave for %.0fs; ending the persistent session'
                      % _WAVE_IDLE_TIMEOUT_SEC)
                return None
            time.sleep(_WAVE_POLL_SEC)

    def _write_done(self, n, results, not_collected):
        tmp = os.path.join(self.control_dir, 'wave-%d.done.tmp' % n)
        final = os.path.join(self.control_dir, 'wave-%d.done.json' % n)
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'results': results, 'not_collected': not_collected}, fh)
        # Atomic: the client polls for this file's existence, so it must never observe a half-written
        # one and conclude the wave produced two results out of twenty.
        os.replace(tmp, final)

    def _boundary_teardown(self, session, next_first, owner):
        """Run the held teardown, and make a failure in it visible instead of fatal.

        Measured on a class fixture whose teardown raises, with the boundary falling between two
        classes:

            vanilla                       rc=1   4 passed, 1 error
            waves, unguarded boundary     rc=3   2 passed          <- INTERNALERROR

        The exception escapes pytest_runtestloop and kills the session, so the next wave's nodeids
        never run and never report. Re-attributing it to the item that owned the fixture reproduces
        vanilla exactly, error row included, and the session survives.
        """
        from _pytest.runner import CallInfo

        call = CallInfo.from_call(
            lambda: session._setupstate.teardown_exact(next_first), when='teardown')
        if call.excinfo is None:
            return
        if owner is None:
            raise call.excinfo.value
        report = owner.ihook.pytest_runtest_makereport(item=owner, call=call)
        owner.ihook.pytest_runtest_logreport(report=report)

    def run(self, session):
        by_id = {item.nodeid: item for item in session.items}
        boundary = _WaveBoundary()
        held_item = None
        n = 0

        session.config.pluginmanager.register(self, 'testhide_wave_runner')
        try:
            while True:
                wave = self._read_wave(n)
                if wave is None:
                    break

                items, not_collected = [], []
                for nodeid in wave:
                    item = by_id.get(nodeid)
                    if item is None:
                        not_collected.append(nodeid)
                    else:
                        items.append(item)

                if items and held_item is not None:
                    self._boundary_teardown(session, items[0], held_item)

                self._wave_reports = {}
                for i, item in enumerate(items):
                    nextitem = items[i + 1] if i + 1 < len(items) else boundary.hold(item)
                    item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
                    held_item = item
                    if session.shouldfail or session.shouldstop:
                        break

                results = []
                for item in items:
                    outcome, duration = self._verdict(item.nodeid)
                    results.append({'nodeid': item.nodeid, 'outcome': outcome,
                                    'duration': round(duration, 6)})
                self._write_done(n, results, not_collected)

                if not_collected:
                    # Loud, because the alternative is a nodeid that was assigned and simply never
                    # spoken of again. The usual cause is an executor started with a narrower scope
                    # than the batches it is being given.
                    print('[testhide] wave %d: %d nodeid(s) not collected in this session: %s'
                          % (n, len(not_collected), ', '.join(not_collected[:5])))

                if session.shouldfail:
                    print('[testhide] session stopping early: %s' % session.shouldfail)
                    break
                if session.shouldstop:
                    print('[testhide] session interrupted: %s' % session.shouldstop)
                    break
                n += 1
        finally:
            session.config.pluginmanager.unregister(self)
            # The one place the session itself unwinds. Guarded: a teardown that raises here must
            # not turn a completed run into an INTERNALERROR.
            try:
                session._setupstate.teardown_exact(None)
            except Exception as exc:
                print('[testhide] error during final teardown: %r' % (exc,))


def _session_control_dir(config):
    """Where the wave protocol lives, or None for an ordinary run.

    The environment variable is the primary source and the option exists for the client-composed
    command line and for tests. A path, not a switch: the same reason --report-xml is a path.
    """
    return (getattr(config.option, 'testhide_session_dir', None)
            or os.environ.get('TESTHIDE_SESSION_DIR') or None)


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    """Run the session as WAVES when a control directory is present; otherwise stand aside.

    tryfirst because pytest_runtestloop is firstresult=True: the first implementation to return a
    value ends the call. Returning None (an ordinary run) lets pytest's own loop proceed untouched.
    """
    control_dir = _session_control_dir(session.config)
    if not control_dir:
        return None

    if session.testsfailed and not session.config.option.continue_on_collection_errors:
        raise session.Failed('%d error(s) during collection' % session.testsfailed)
    if session.config.option.collectonly:
        return True

    _WaveRunner(control_dir).run(session)
    return True


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    try:
        config.pluginmanager.add_hookspecs(hookspecs)
    except pluggy.PluginValidationError:
        pass

    # A persistent session IS a scheduled batch — several of them — so it inherits the batch guard
    # rather than restating it. That is not tidiness: -x is fatal here in a way it is not per-batch.
    # Measured, two waves of two nodeids with a failure in the first:
    #     guard applied   2/2 waves ran, all 4 nodeids reported
    #     -x left in      0/2 waves completed; the session died and wave 1 was never even delivered
    if _session_control_dir(config):
        config.option.testhide_batch = True

    # Before the --report-xml gate below: a batch must be neutralised whether or not this run also
    # produces a report.
    if getattr(config.option, 'testhide_batch', False):
        _neutralise_batch_argv(config)

    if not config.option.report_xml:
        return
    
    base_name = "testhide_plugin"
    
    if config.pluginmanager.has_plugin(base_name + "_active"):
        return
    
    config.pluginmanager.register(TesthidePlugin(config), base_name + "_active")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Module-level hook that always fires (even without --report-xml).
    Computes fail_id on test failure and prints it to console/log.
    """
    outcome = yield
    report = outcome.get_result()
    
    if call.when == 'call' and call.excinfo:
        fail_message = str(call.excinfo.value)
        try:
            fail_id = compute_fail_id(
                item.module.__name__,
                item.cls.__name__ if item.cls else item.module.__name__,
                item.name,
                call.excinfo.typename,
                fail_message,
            )
        except AttributeError:
            return
        item.fail_id = fail_id
        
        # Always log fail_id to console
        jira_info = ''
        active_plugin = item.config.pluginmanager.get_plugin('testhide_plugin_active')
        if active_plugin and active_plugin.jira_enabled and active_plugin.jira:
            try:
                issue = active_plugin._get_issue_by_test_id(fail_id)
                if issue:
                    jira_info = (f' | jira={issue.key}'
                                 f' | status={issue.fields.status.name}'
                                 f' | summary={issue.fields.summary}')
            except Exception:
                pass
        print(f'[testhide_fail_info][{item.nodeid}]: fail_id={fail_id}{jira_info}')


QUARANTINE_DEFAULT_FILENAME = '.testhide_quarantine_file'


def _resolve_quarantine_path(config):
    """
    Resolve the quarantine file path.
    
    Priority:
      1. --quarantine-file CLI option
      2. TESTHIDE_QUARANTINE_FILE environment variable
      3. .testhide_quarantine_file in the working directory (auto-discovery)
    
    Supports both absolute and relative paths (relative = resolved against cwd).
    Returns None if no quarantine file exists.
    """
    explicit = getattr(config.option, 'quarantine_file', None) or os.environ.get('TESTHIDE_QUARANTINE_FILE')
    
    if explicit:
        # Support both absolute and relative paths
        resolved = os.path.abspath(explicit)
        if os.path.isfile(resolved):
            return resolved
        # Not found — don't fall back, user explicitly set a path
        print(f'[testhide_quarantine] Quarantine file not found: {resolved}')
        return None
    
    # Auto-discovery: check default file in working directory
    default_path = os.path.join(os.getcwd(), QUARANTINE_DEFAULT_FILENAME)
    if os.path.isfile(default_path):
        return default_path
    
    # Also check rootdir (pytest may change cwd)
    rootdir = str(getattr(config, 'rootdir', ''))
    if rootdir:
        rootdir_path = os.path.join(rootdir, QUARANTINE_DEFAULT_FILENAME)
        if os.path.isfile(rootdir_path):
            return rootdir_path
    
    return None


def pytest_collection_modifyitems(config, items):
    """
    Quarantine hook: deselects quarantined tests before execution.
    
    Reads quarantined nodeids from a file. File resolution:
      1. --quarantine-file CLI option (absolute or relative path)
      2. TESTHIDE_QUARANTINE_FILE environment variable (absolute or relative)
      3. .testhide_quarantine_file in working directory (auto-discovery)
    
    The file is written by the C# agent before running pytest,
    containing one quarantined nodeid per line.
    
    Supports both exact match and prefix match:
      - "tests/test_login.py::TestLogin" matches all tests in that class
      - "tests/test_login.py::TestLogin::test_flaky" matches exactly
    """
    quarantine_file = _resolve_quarantine_path(config)
    
    if not quarantine_file:
        return
    
    try:
        with open(quarantine_file, 'r', encoding='utf-8') as f:
            quarantined_nodeids = {
                line.strip() for line in f if line.strip() and not line.startswith('#')
            }
    except Exception as e:
        print(f'[testhide_quarantine] Failed to read quarantine file {quarantine_file}: {e}')
        return
    
    if not quarantined_nodeids:
        return
    
    remaining = []
    deselected = []
    
    for item in items:
        is_quarantined = False
        
        # Check exact match first
        if item.nodeid in quarantined_nodeids:
            is_quarantined = True
        else:
            # Check prefix match (class-level quarantine → all tests in class)
            for qid in quarantined_nodeids:
                if item.nodeid.startswith(qid + '::') or item.nodeid.startswith(qid + '['):
                    is_quarantined = True
                    break
        
        if is_quarantined:
            deselected.append(item)
        else:
            remaining.append(item)
    
    if not deselected:
        return

    # RA-3. Under --testhide-batch these nodeids were ASSIGNED to this executor by the scheduler,
    # and deselecting them produces exactly the outcome the batch guard exists to eliminate: a test
    # that neither runs, nor reports, nor fails. It is not re-queued (no result was reported, and it
    # was not failed), so the heartbeat sweep hands it to another executor, which quarantines it
    # too — forever. Measured before the fix: `--testhide-batch --quarantine-file=q.txt` over 3
    # explicit nodeids gave "2 passed, 1 deselected" and a junit containing only two of the three.
    #
    # Skipping instead of deselecting keeps the quarantine decision (the test does not execute) while
    # producing a RESULT for it: a <testcase> with <skipped> and the reason, which the scheduler
    # accounts for and the report shows honestly. Outside a batch nothing changes — an ordinary run
    # has no scheduler waiting on those nodeids, and deselection keeps the summary clean.
    if getattr(config.option, 'testhide_batch', False):
        for item in deselected:
            item.add_marker(pytest.mark.skip(
                reason='testhide: quarantined (%s)' % os.path.basename(quarantine_file)))
        print(
            f'[testhide_quarantine] Skipping {len(deselected)} quarantined tests '
            f'(from {quarantine_file}): reported as skipped rather than dropped, because the '
            f'TestHide scheduler assigned them to this batch and is waiting for a result'
        )
        return

    config.hook.pytest_deselected(items=deselected)
    items[:] = remaining
    print(
        f'[testhide_quarantine] Deselected {len(deselected)} quarantined tests '
        f'(from {quarantine_file}), {len(remaining)} remaining'
    )
