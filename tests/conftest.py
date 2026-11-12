import sys
from pathlib import Path

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    on_windows = sys.platform == "win32"
    skip_posix = pytest.mark.skip(reason="POSIX-only test")
    skip_windows = pytest.mark.skip(reason="Windows-only test")
    for item in items:
        if on_windows and "posix_only" in item.keywords:
            item.add_marker(skip_posix)
        elif not on_windows and "windows_only" in item.keywords:
            item.add_marker(skip_windows)


def make_symlink(
    link: Path, target: Path, *, target_is_directory: bool = False
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError:
        if sys.platform == "win32":
            pytest.skip("symlink creation not permitted on this platform")
        raise
