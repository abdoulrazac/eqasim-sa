#!/usr/bin/env python3
import argparse
import zipfile
from pathlib import Path


REQUIRED_TEMPLATES = {
    "eqasim_sa/templates/00-calibration.yml",
    "eqasim_sa/templates/01-grid.yml",
    "eqasim_sa/templates/02-discrete-oat.yml",
    "eqasim_sa/templates/03-sobol.yml",
    "eqasim_sa/templates/README.md",
    "eqasim_sa/templates/__init__.py",
}
FORBIDDEN_PARTS = {"docs", "docs-fr", "protocols"}


def verify_wheel(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"wheel is not a regular file: {path}")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    templates = {name for name in names if name.startswith("eqasim_sa/templates/")}
    if templates != REQUIRED_TEMPLATES:
        missing = sorted(REQUIRED_TEMPLATES - templates)
        unexpected = sorted(templates - REQUIRED_TEMPLATES)
        raise ValueError(
            f"wheel template boundary mismatch: missing={missing}, unexpected={unexpected}"
        )
    forbidden = sorted(
        name for name in names if FORBIDDEN_PARTS.intersection(Path(name).parts)
    )
    if forbidden:
        raise ValueError(f"wheel contains repository-only resources: {forbidden}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the eqasim-sa release wheel resource boundary."
    )
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    verify_wheel(args.wheel)
    print(f"verified wheel resource boundary: {args.wheel}")


if __name__ == "__main__":
    main()
