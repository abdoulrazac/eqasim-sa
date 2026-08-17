from importlib.resources import files


def nantes_template() -> str:
    return (
        files(__package__)
        .joinpath("nantes-meta-config.yml")
        .read_text(encoding="utf-8")
    )
