# Testhide Pytest Plugin

A professional-grade pytest plugin that generates robust JUnit-style XML reports. This plugin was designed to solve real-world CI/CD challenges by ensuring data integrity during test failures and providing full support for parallel test execution.

## Key Features

* **Incremental Reporting**: Every single test result is saved immediately, guaranteeing that partial results are available even if a test run is catastrophically interrupted.
* **Full `pytest-xdist` Compatibility**: The plugin uses a robust temporary file and merge strategy, enabling it to work flawlessly with parallel test execution (`-n X`).
* **`pytest-rerunfailures` Support**: Every rerun of a failing test is logged as a separate `<testcase>` in the final report, providing a complete and accurate picture of flaky tests.
* **JIRA Integration**: Automatically enriches failure reports with information from JIRA, linking test failures to known bugs and their statuses.
* **Clean Stack Traces**: Automatically removes internal "noise" from pytest and pluggy calls in stack traces, leaving only the relevant information from your application and test code.
* **Atomic & Safe Writes**: Uses a temporary directory and a final, atomic merge to ensure the report file is never corrupted, even under heavy load or across multiple concurrent builds on the same agent.

## Installation

```bash
pip install testhide-pytest-plugin
```

## Usage
### Basic Run
To activate the plugin and generate a report, use the --report-xml option:
```bash
pytest --report-xml=junittests.xml
```

## Parallel Execution (pytest-xdist)
The plugin is fully compatible with `pytest-xdist` out of the box. Simply add the -n flag to run tests in multiple processes. The plugin will automatically handle and merge the results from all worker nodes.
```bash
pytest -n auto --report-xml=junittests.xml
```

## Rerunning Failed Tests (pytest-rerunfailures)
The plugin works seamlessly with `pytest-rerunfailures`. Every attempt of a failing test will be recorded in the final report, allowing for accurate tracking of test instability.
```bash
pytest --reruns 5 --report-xml=junittests.xml
```

## JIRA Integration
The plugin can automatically enrich failure reports with information from JIRA, linking test failures to known bugs and their statuses. There are two ways to configure this integration.

### Method 1: Command-Line Arguments
You can enable JIRA integration by providing the connection details as command-line options. The integration is activated automatically when all three parameters are present.

* **--jira-url**: The URL of your JIRA instance.
* **--jira-username**: The username for the connection.
* **--jira-password**: The password or API token for the user.
```bash
pytest --report-xml=junittests.xml \
       --jira-url="[https://jira.yourcompany.com](https://jira.yourcompany.com)" \
       --jira-username="my-bot" \
       --jira-password="your-api-token"
```

### Method 2: Programmatic Configuration (for Frameworks)
If you are developing a test framework plugin and manage credentials in a central configuration object (e.g., a YAML file), you can programmatically set the JIRA options. This avoids exposing credentials in CI scripts.
Use the `pytest_cmdline_main` hook in your own plugin to set the configuration options before the `testhide-plugin` is configured.
```python
import pytest

class MyFrameworkPlugin:
    @pytest.hookimpl(tryfirst=True)
    def pytest_cmdline_main(self, config):
        # Assuming ConfigApp loads your central configuration
        # from a file or environment variables.
        from my_framework.config import ConfigApp
        
        config.option.jira_url = ConfigApp.jira.url
        config.option.jira_username = ConfigApp.jira.username
        config.option.jira_password = ConfigApp.jira.password
```

## Extending the Plugin (For Framework Developers)
`testhide-pytest-plugin` provides custom hooks for integration with your own plugins, allowing you to inject project-specific metadata into the report.

## Example implementation in your plugin:
### `pytest_testhide_add_metadata(plugin)`
This hook allows you to add metadata at the session level (e.g., build information, branch name, etc.). It must return a list of `(name, value)` tuples.

```python
from pytest import hookimpl

class MyFrameworkPlugin:
    @hookimpl
    def pytest_testhide_add_metadata(self, plugin):
        return [
            ('build', '1.2.3'),
            ('branch', 'develop')
        ]
```


### `pytest_testhide_get_test_case_properties(item, report)`
This hook allows you to add data at the individual test case level (e.g., a docstring, steps to reproduce, or artifact links). It must return a list of `(name, value)` tuples.

```python
from pytest import hookimpl

class MyFrameworkPlugin:
    @hookimpl
    def pytest_testhide_get_test_case_properties(self, item, report):
        properties = []
        if item.obj and item.obj.__doc__:
            properties.append(('docstr', item.obj.__doc__.strip()))
        
        # Example of adding an artifact link
        if hasattr(item, 'artifact_url'):
            properties.append(('attachment', item.artifact_url))
            
        return properties
```