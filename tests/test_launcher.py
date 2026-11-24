import ast
import tomllib
from pathlib import Path

from eqasim_sa import launcher

ROOT = Path(__file__).resolve().parents[1]


def test_supported_interpreter_has_no_error():
    assert launcher.interpreter_error((3, 13, 11, "final", 0), "/x/python") is None


def test_other_interpreter_is_named_with_the_reinstall_command():
    message = launcher.interpreter_error((3, 11, 11, "final", 0), "/tool/bin/python")

    assert message == (
        "eqasim-sa requires Python 3.13, but this environment runs Python 3.11.11 "
        "(/tool/bin/python). Reinstall the tool with Python 3.13: "
        "uv tool uninstall eqasim-sa && "
        "uv tool install --python 3.13 git+https://github.com/abdoulrazac/eqasim-sa@latest"
    )


def test_main_refuses_another_interpreter_before_loading_the_application(
    monkeypatch, capsys
):
    monkeypatch.setattr(launcher.sys, "version_info", (3, 11, 11, "final", 0))

    def forbidden(*args, **kwargs):
        raise AssertionError("the application must not be imported")

    monkeypatch.setattr("builtins.__import__", forbidden)
    code = launcher.main()
    monkeypatch.undo()

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "runs Python 3.11.11" in captured.err


def test_launcher_imports_only_the_standard_library():
    tree = ast.parse((ROOT / "src/eqasim_sa/launcher.py").read_text(encoding="utf-8"))
    top_level = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]

    assert [alias.name for node in top_level for alias in node.names] == ["sys"]


def test_console_script_goes_through_the_launcher():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["eqasim-sa"] == "eqasim_sa.launcher:main"
