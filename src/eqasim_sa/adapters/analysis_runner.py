"""Run one analysis script inside eqasim's Python interpreter.

This file is self-contained: it uses only the standard library and pandas,
parses as Python 3.9 and never imports eqasim-sa.  eqasim-sa copies it into a
temporary directory and launches it with eqasim's interpreter, so the
directory of eqasim-sa's own modules is never on ``sys.path``.

Usage::

    python analysis_runner.py <request.json>
    python analysis_runner.py --probe <versions.json>

The UTF-8 JSON request is ``{script, files, parameters, output}``.  The script
must define ``compute(files, parameters)`` returning a ``pandas.DataFrame``
whose column names are unique, non-empty strings; the table is written to
``output`` as a comma-separated UTF-8 CSV without index.  On failure the exit
status is non-zero and the last line of standard error states the cause.
"""

import contextlib
import importlib
import importlib.util
import json
import sys
import traceback


def _report_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def _version(name):
    try:
        module = importlib.import_module(name)
    except ImportError:
        return None
    version = getattr(module, "__version__", None)
    return version if isinstance(version, str) else None


def _probe(destination):
    versions = {
        "python": sys.version,
        "pandas": _version("pandas"),
        "numpy": _version("numpy"),
        "pyarrow": _version("pyarrow"),
    }
    with open(destination, "w", encoding="utf-8") as stream:
        json.dump(versions, stream, ensure_ascii=False)
    return 0


def _load(script):
    spec = importlib.util.spec_from_file_location("eqasim_analysis_script", script)
    if spec is None or spec.loader is None:
        raise ImportError("analysis script cannot be loaded: " + script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate(table):
    import pandas

    if not isinstance(table, pandas.DataFrame):
        raise TypeError(
            "compute must return a pandas.DataFrame, not "
            + type(table).__name__
        )
    names = list(table.columns)
    if not names:
        raise ValueError("compute must return a table with at least one column")
    seen = set()
    for name in names:
        if not isinstance(name, str) or not name:
            raise ValueError(
                "compute must return non-empty text column names, not "
                + repr(name)
            )
        if name in seen:
            raise ValueError("compute returned a duplicate column name: " + repr(name))
        seen.add(name)


def _run(request_path):
    with open(request_path, "r", encoding="utf-8") as stream:
        request = json.load(stream)
    # Anything the script prints goes to the error log, never to stdout.
    with contextlib.redirect_stdout(sys.stderr):
        module = _load(request["script"])
        compute = getattr(module, "compute", None)
        if not callable(compute):
            raise TypeError("analysis script must define compute(files, parameters)")
        table = compute(dict(request["files"]), dict(request["parameters"]))
    _validate(table)
    with open(request["output"], "w", encoding="utf-8", newline="") as stream:
        table.to_csv(stream, index=False)
    return 0


def main(argv):
    _report_utf8()
    if len(argv) == 3 and argv[1] == "--probe":
        return _probe(argv[2])
    if len(argv) != 2:
        sys.stderr.write("usage: analysis_runner.py <request.json>\n")
        return 2
    try:
        return _run(argv[1])
    except BaseException:
        traceback.print_exc()
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
