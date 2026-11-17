"""Shared process exit codes for the orcexe CLI.

Use these constants as command return values to distinguish successful execution
from usage errors and specific operational failures.
"""

OK = 0
"""The command completed successfully."""
FAIL = 1
"""The command failed."""
USAGE = 2
"""The command-line arguments are invalid or incomplete."""
NOT_FOUND = 3
"""A requested resource could not be found."""
AUTH = 4
"""Authentication or authorization failed."""
QUOTA = 5
"""A resource quota was exceeded."""
TIMEOUT = 6
"""The operation exceeded its time limit."""
UNSUPPORTED = 7
"""The requested operation is unsupported."""
INTERNAL = 70
"""An internal error occurred."""
