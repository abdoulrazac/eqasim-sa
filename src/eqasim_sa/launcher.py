"""Console entry point that checks the interpreter before loading the application.

This module only imports the standard library, so that a tool environment whose
Python does not match the installed packages (for example a uv tool environment
whose interpreter link now points to another Python version) reports a clear
message instead of an import error from a compiled dependency.
"""

import sys

REQUIRED_PYTHON = (3, 13)
REINSTALL_COMMAND = (
    "uv tool uninstall eqasim-sa && "
    "uv tool install --python 3.13 git+https://github.com/abdoulrazac/eqasim-sa@latest"
)


def interpreter_error(version=None, executable=None):
    """Return the message for an unsupported interpreter, or None when it is supported."""

    version = sys.version_info if version is None else version
    executable = sys.executable if executable is None else executable
    if tuple(version[:2]) == REQUIRED_PYTHON:
        return None
    running = ".".join(str(part) for part in version[:3])
    return (
        "eqasim-sa requires Python 3.13, but this environment runs Python "
        + running
        + " ("
        + str(executable)
        + "). Reinstall the tool with Python 3.13: "
        + REINSTALL_COMMAND
    )


def main():
    """Check the interpreter, then run the eqasim-sa command line."""

    error = interpreter_error()
    if error is not None:
        sys.stderr.write(error + "\n")
        return 1
    from eqasim_sa.adapters.application import main as application_main

    return application_main()
