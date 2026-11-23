"""Static guard: pyarrow reads must never use arrow's worker threads.

A threaded read can leave a worker thread parked on the GIL while Python
shuts down, and the process then hangs forever at exit.  Parquet reads go
through ``eqasim_sa.utils.arrow.read_parquet``; CSV reads pass a
``ReadOptions(use_threads=False)``.  Outside the helper, ``ParquetFile`` and
``read_table`` are forbidden except in metadata-only functions named below.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = "src/eqasim_sa/utils/arrow.py"

# (file, enclosing function) pairs that only open Parquet metadata.
METADATA_ONLY = {
    ("src/eqasim_sa/core/ledger.py", "<module>"),  # _PARQUET_FILE alias
    ("src/eqasim_sa/core/ledger.py", "completion_state"),
    ("src/eqasim_sa/core/campaign_snapshot.py", "inspect_plan"),
    ("src/eqasim_sa/adapters/application.py", "run_from_meta_config"),
}

BANNED_ATTRIBUTES = {
    "read_table",
    "iter_batches",
    "read_row_group",
    "read_row_groups",
    "ParquetDataset",
    "ParquetFile",
    "_PARQUET_FILE",
}
READ_RECEIVERS = {"parquet", "parquet_file", "pf"}


def _sources():
    for base in ("src", "protocols"):
        yield from sorted((ROOT / base).rglob("*.py"))


def _name(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _no_threads(call):
    return any(
        keyword.arg == "use_threads"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _scan(rel, tree):
    """Return violations of one parsed file whose repo-relative path is rel."""
    found = []
    helper = rel == HELPER

    def report(node, text):
        found.append(f"{rel}:{node.lineno}: {text}")

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.ImportFrom) and node.module in {
            "pyarrow.parquet",
            "pyarrow.csv",
            "pyarrow.dataset",
        }:
            report(node, f"import names from {node.module}; use the module alias")
        if isinstance(node, ast.Import):
            for item in node.names:
                expected = {"pyarrow.parquet": "pq", "pyarrow.csv": "csv"}
                if item.name == "pyarrow.dataset":
                    report(node, "pyarrow.dataset is forbidden")
                elif item.name in expected and item.asname != expected[item.name]:
                    report(node, f"import {item.name} as {expected[item.name]}")
        if isinstance(node, ast.Attribute) and node.attr == "dataset":
            if _name(node.value) in {"pa", "pyarrow", "ds"}:
                report(node, "pyarrow.dataset is forbidden")
        if isinstance(node, ast.Name) and node.id == "ds":
            report(node, "pyarrow.dataset alias is forbidden")
        name = _name(node) if isinstance(node, (ast.Attribute, ast.Name)) else None
        if name in BANNED_ATTRIBUTES and not helper:
            if (rel, function) not in METADATA_ONLY:
                report(node, f"{name} outside eqasim_sa.utils.arrow.read_parquet")
        if isinstance(node, ast.Call):
            callee = _name(node.func)
            if callee == "read" and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                threaded = (
                    isinstance(receiver, ast.Call)
                    and _name(receiver.func) in {"ParquetFile", "_PARQUET_FILE"}
                ) or _name(receiver) in READ_RECEIVERS
                if threaded and not (helper and _no_threads(node)):
                    report(node, "Parquet .read() outside the helper")
            elif callee == "ReadOptions" and not _no_threads(node):
                report(node, "ReadOptions needs use_threads=False")
            elif callee in {"read_csv", "open_csv"} and not any(
                keyword.arg == "read_options" for keyword in node.keywords
            ):
                report(node, f"{callee} needs read_options with use_threads=False")
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def test_pyarrow_reads_never_use_threads():
    found = []
    for path in _sources():
        rel = path.relative_to(ROOT).as_posix()
        found += _scan(rel, ast.parse(path.read_text(encoding="utf-8")))
    assert not found, "\n".join(found)


SNIPPETS = [
    ("pf = pq.ParquetFile(x)\npf.read()", True),
    ("from pyarrow.parquet import read_table as rt\nrt(x)", True),
    ("import pyarrow.parquet as parq\n", True),
    ("import pyarrow.csv as pacsv\n", True),
    ("pq.read_table(x)", True),
    ("pf.iter_batches()", True),
    ("pf.read_row_group(0)", True),
    ("pq.ParquetDataset(x)", True),
    ("import pyarrow.dataset as ds\n", True),
    ("pa.dataset.dataset(x)", True),
    ("csv.read_csv(s)", True),
    ("csv.open_csv(s)", True),
    ("csv.ReadOptions(block_size=1)", True),
    ("csv.ReadOptions()", True),
    ("read_parquet(x)\nread_parquet(x, columns=['a'])", False),
    ("csv.ReadOptions(use_threads=False)", False),
    ("csv.read_csv(s, read_options=r)", False),
    ("import pyarrow.parquet as pq\nimport pyarrow.csv as csv\n", False),
]


def test_guard_flags_exactly_the_threaded_snippets():
    for source, expected in SNIPPETS:
        flagged = bool(_scan("src/eqasim_sa/x.py", ast.parse(source)))
        assert flagged is expected, source


def test_guard_allow_list_is_per_function():
    source = "def f():\n    return pq.ParquetFile(x).metadata\n"
    path = "src/eqasim_sa/core/ledger.py"
    assert _scan(path, ast.parse(source))
    allowed = source.replace("def f", "def completion_state")
    assert not _scan(path, ast.parse(allowed))
    assert _scan("src/eqasim_sa/core/other.py", ast.parse(allowed))
    helper = "def r(s):\n    return pq.ParquetFile(s).read(use_threads=False)\n"
    assert not _scan(HELPER, ast.parse(helper))
    assert _scan(HELPER, ast.parse(helper.replace("use_threads=False", "")))
