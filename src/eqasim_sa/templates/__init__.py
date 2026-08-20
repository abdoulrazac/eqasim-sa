from importlib.resources import files


NAMES = (
    "README.md",
    "00-calibration.yml",
    "01-grid.yml",
    "02-discrete-oat.yml",
    "03-sobol.yml",
)


def project_templates() -> dict[str, str]:
    root = files(__package__)
    return {name: root.joinpath(name).read_text(encoding="utf-8") for name in NAMES}
