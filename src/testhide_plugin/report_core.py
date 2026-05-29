"""
Shared report-format constants/helpers for the Testhide pytest plugin.

Kept tiny and dependency-free. The fail_id formula here is byte-for-byte identical to the
historical inline computation in plugin.py (do NOT change it — fail_id is a stable dedup/Jira
key used by the backend across builds).
"""
import re
from hashlib import md5

# Testhide Report Format version, emitted as a <testsuite> property.
# See https://github.com/thuesdays/testhide/blob/main/docs/specs/REPORT-FORMAT-V1.md
SCHEMA_VERSION = "1"

_PARAM_SUFFIX = re.compile(r'\[.+\]$')


def compute_fail_id(module, cls, name, exc_type, message):
    """md5("module.cls.name.ExceptionType(message)"); pytest parametrization [..] is stripped."""
    name = _PARAM_SUFFIX.sub('', name or '')
    raw = '%s.%s.%s.%s(%s)' % (module or '', cls or '', name, exc_type or '', message or '')
    return md5(raw.encode('utf-8')).hexdigest()
