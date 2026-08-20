import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def option(name: str) -> str:
    index = sys.argv.index(name)
    return sys.argv[index + 1]


def write(
    path: Path, delimiter: str, columns: list[str], rows: list[list[object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=delimiter)
        writer.writerow(columns)
        writer.writerows(rows)


def main() -> int:
    if (
        len(sys.argv) == 2
        and Path(sys.argv[1]).name.startswith(".eqasim-sa-probe-")
    ):
        print(f"synpp.general.PipelineError: Config file does not exist: {sys.argv[1]}", file=sys.stderr)
        return 1
    if sys.argv[1:] == ["--help"]:
        print("usage: eqasim-spy CONFIG [--key value ...]")
        return 0
    output = Path(option("--output_path"))
    prefix = option("--output_prefix")
    output.mkdir(parents=True, exist_ok=True)
    record = Path(os.environ["EQASIM_SPY_RECORD"])
    child = None
    if os.environ.get("EQASIM_SPY_CHILD_RECORD"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(os.environ["EQASIM_SPY_CHILD_RECORD"]).write_text(str(child.pid))
    record.write_text(
        json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid()})
    )
    if os.environ.get("EQASIM_SPY_SLEEP"):
        time.sleep(float(os.environ["EQASIM_SPY_SLEEP"]))
    if os.environ.get("EQASIM_SPY_BARRIER"):
        barrier = Path(os.environ["EQASIM_SPY_BARRIER"])
        barrier.mkdir(exist_ok=True)
        marker = barrier / str(os.getpid())
        marker.touch()
        if len(tuple(barrier.iterdir())) > 1:
            (barrier.parent / "overlap").touch()
        time.sleep(0.1)
        marker.unlink()
    print("spy stdout")
    print("spy stderr", file=sys.stderr)
    fail_seed = os.environ.get("EQASIM_SPY_FAIL_SEED") == option("--random_seed")
    fail_rate = os.environ.get("EQASIM_SPY_FAIL_SAMPLING_RATE")
    if fail_seed and (fail_rate is None or fail_rate == option("--sampling_rate")):
        return 7
    if os.environ.get("EQASIM_SPY_FAIL"):
        return int(os.environ["EQASIM_SPY_FAIL"])
    case = os.environ.get("EQASIM_SPY_CASE", "")
    population = (
        "persons",
        "households",
        "activities",
        "trips",
        "vehicle_types",
        "vehicles",
    )
    for name in population:
        write(
            output / f"{prefix}{name}.csv",
            ";",
            ["id", "label"],
            [[1, "65+"], [2, None]],
    )
    analysis = ("age", "distance", "license", "nbr_vehicle", "travel")
    for name in analysis:
        write(
            output / "analysis_population" / f"{prefix}{name}.csv",
            ",",
            [
                "",
                "Age class",
                "HTS adjust with INSEE population weights",
                "Proportion_EQASIM",
            ],
            [[0, "65+", 0.5, 0.4], [1, "(99.0, inf]", None, 0.6]],
        )
    write(
        output / "analysis_population" / f"{prefix}age_purpose.csv",
        ",",
        ["age_class", "Home<>Work"],
        [["65+", 2]],
    )
    age = output / "analysis_population" / f"{prefix}age.csv"
    if case == "nonsequential-index":
        write(age, ",", ["", "Age class"], [[1, "65+"]])
    elif case == "duplicate-empty-header":
        write(age, ",", ["", "", "Age class"], [[0, 1, "65+"]])
    elif case == "malformed-csv":
        age.write_text(',"unterminated\n', encoding="utf-8")
    (output / "stale.csv").write_text("ignored\n", encoding="utf-8")
    (output / "analysis_population" / "other_age.csv").write_text(
        "ignored\n", encoding="utf-8"
    )
    omitted = os.environ.get("EQASIM_SPY_OMIT")
    if omitted:
        target = output / "analysis_population" / f"{prefix}{omitted}.csv"
        target.unlink(missing_ok=True)
    if child is not None:
        child.terminate()
        child.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
