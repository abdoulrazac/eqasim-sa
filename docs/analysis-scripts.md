# Writing an analysis script

**English** | [Français](../docs-fr/analysis-scripts.md)

When you launch a campaign, you do not always know which quantities you will
want to study. An analysis script lets you add one afterwards: a short Python
file reads the archived files of each run and returns a table. That table
becomes an *analysis output*, which the `tv_distance`, `share`, and `value`
indicators read like any other output.

Computing an analysis output never runs eqasim or synpp, never reads the synpp
cache, and needs no input data: it reads only the run archives. The plan and
the archives are never modified. You can therefore run every simulation on a
cluster, copy the campaign folder back to your computer, write scripts there,
and run `analyze`.

## Contents

- [Before the runs: archive enough](#before-the-runs-archive-enough)
- [A complete example](#a-complete-example)
- [The script contract](#the-script-contract)
- [Rules for a script](#rules-for-a-script)
- [Which Python runs the script](#which-python-runs-the-script)
- [What `analyze` does](#what-analyze-does)
- [Trying a script by hand](#trying-a-script-by-hand)
- [Cache and recomputation](#cache-and-recomputation)
- [After a campaign on a cluster](#after-a-campaign-on-a-cluster)
- [What is recorded](#what-is-recorded)
- [Limits](#limits)

## Before the runs: archive enough

A script can only read files that each run archived, that is, outputs
declared under [`outputs`](yaml-reference.md#outputs) before the campaign is
planned. Adding an output afterwards needs a new campaign. So archive
generously from the start: the population (`persons`, `households`), the trips
and activities (`trips`, `activities`), and, if you may need distances or
places, the spatial files written by eqasim:

```yaml
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  trips_spatial:
    file: "{prefix}trips.gpkg"
  activities_spatial:
    file: "{prefix}activities.gpkg"
```

eqasim writes the `.gpkg` files when `output_formats` of its configuration
contains `gpkg`, which is its default.

## A complete example

This example adds two quantities to a Grid campaign: the mean home-to-work
distance of each run, and the share of trips by mode. The project looks like
this:

```text
mon-analyse-sa/
├── configs/
│   ├── 01-grid.yml
│   └── analyses/
│       ├── commute.py
│       └── mode_share.py
└── campaigns/
    └── mon-analyse-sa-grid/
```

The meta-configuration declares the outputs the scripts read, the two
analyses, and the indicators that use them:

```yaml
outputs:
  persons: {}
  trips: {}
  trips_spatial:
    file: "{prefix}trips.gpkg"
analyses:
  commute:
    script: analyses/commute.py        # relative to this YAML file
    inputs: [trips_spatial, persons]   # archived outputs given to the script
  mode_share:
    script: analyses/mode_share.py
    inputs: [trips]
    category: mode                     # columns of the table the script returns
    simulated: share
indicators:
  - value: {output: commute, column: mean_commute_km, where: {group: all}}
  - share: {output: mode_share, category: car}
```

The [YAML reference](yaml-reference.md#analyses) describes every key of
`analyses`, and its [complete example](yaml-reference.md#grid-with-an-analysis-script)
shows a whole file.

`configs/analyses/commute.py` measures the straight-line length of every trip
from home to work, in kilometres, and averages it for all commuters and for
each sex:

```python
"""Mean home-to-work distance of one run, for all commuters and by sex."""

import geopandas
import pandas


def compute(files, parameters):
    trips = geopandas.read_file(files["trips_spatial"])
    persons = pandas.read_csv(files["persons"], sep=";", usecols=["person_id", "sex"])

    if trips.crs is None or not trips.crs.is_projected:
        raise ValueError("trips_spatial must use projected coordinates in metres")

    is_commute = (trips["preceding_purpose"] == "home") & (
        trips["following_purpose"] == "work"
    )
    commutes = pandas.DataFrame(
        {
            "person_id": trips.loc[is_commute, "person_id"].to_numpy(),
            "distance_km": trips.loc[is_commute].geometry.length.to_numpy() / 1000.0,
        }
    ).merge(persons, on="person_id")
    if commutes.empty:
        raise ValueError("no home-to-work trip in this run")

    groups = [("all", commutes)] + [
        (str(sex), table) for sex, table in commutes.groupby("sex")
    ]
    return pandas.DataFrame(
        {
            "group": [name for name, _ in groups],
            "commuters": [len(table) for _, table in groups],
            "mean_commute_km": [table["distance_km"].mean() for _, table in groups],
        }
    )
```

For one run of a small Nantes population, it returns:

```text
group,commuters,mean_commute_km
all,460,9.696102057336434
female,208,8.721819935082106
male,252,10.500271428086036
```

The indicator `value(commute, mean_commute_km, group=all)` reads the first
row: 9.70 km for that run.

`configs/analyses/mode_share.py` counts the trips of each mode:

```python
"""Share of trips by mode in one run."""

import pandas


def compute(files, parameters):
    trips = pandas.read_csv(files["trips"], sep=";")
    if "mode" not in trips.columns:
        raise ValueError(
            "trips has no mode column; set mode_choice: true in the eqasim configuration"
        )
    counts = trips["mode"].astype(str).value_counts().sort_index()
    return pandas.DataFrame(
        {"mode": counts.index, "share": counts.to_numpy() / counts.sum()}
    )
```

eqasim writes a `mode` column in the trips file only when its configuration
sets `mode_choice: true`; otherwise this script stops with the message it
raises, and `analyze` reports it (see [What `analyze` does](#what-analyze-does)).
Because the analysis declares `category: mode` and `simulated: share`, the
`share(mode_share, car)` indicator reads the share of car trips. With a
reference column in the returned table (for example survey shares written as
constants in the script) and `references` in the analysis, `tv_distance` would
compare the two distributions.

## The script contract

A script is a `.py` file that defines one function:

```python
def compute(files, parameters):
    ...
    return table  # a pandas.DataFrame
```

`files` maps each name listed in `inputs` to the path of a copy of the
archived file, for example `{"trips_spatial": ".../trips_spatial.gpkg",
"persons": ".../persons.csv"}`. The copy keeps the archive name,
`<output name><extension>`. Read the file with the separator eqasim used: `;`
for the population files (`persons`, `trips`, ...), `,` for the
`analysis_population` files; `geopandas.read_file` reads `.gpkg` files.

`parameters` describes the run:

```python
{
    "runId": "0e599693c300002e9a1c0dffe06ff0ff",
    "seed": 1201,
    "factors": {"sampling_rate": 0.001},
}
```

`factors` gives the value of each varied parameter; for a
[named configuration](yaml-reference.md#named-configurations) it gives the
label, not the payload. Most scripts ignore `parameters`; they are there when
the computation depends on the run (for example to rescale a count by the
`sampling_rate`).

The returned value must be a `pandas.DataFrame` with at least one column, and
column names that are unique, non-empty strings. eqasim-sa writes it as a CSV
file (comma separator, no index, UTF-8) and reads it back with the same rules
as the other outputs: cells are read as text, and the columns an indicator uses
as numbers are converted explicitly. A missing value (`NaN`) becomes an empty
cell, which `value`, `share`, and `tv_distance` refuse with an error naming the
column; raise a clear error in the script instead, as `commute.py` does when a
run has no commute.

## Rules for a script

1. **Do not import eqasim modules.** The result fingerprint covers the script,
   its inputs, the run parameters, and the Python environment, not the eqasim
   code. A script that imported eqasim code would not be recomputed when that
   code changes. Use pandas, numpy, geopandas, and the standard library.
2. **Read only the files in `files`.** A file read from elsewhere is not part
   of the fingerprint, so its changes would go unnoticed. Constants written in
   the script (reference values, class bounds) are fine: they are part of the
   script.
3. **Write nothing: return the table.** The input files are temporary copies,
   deleted after the script ends, and anything written elsewhere is neither
   kept nor traced. The archives are never exposed to the script.
4. **Keep the script in one file.** eqasim-sa runs a copy of the script from a
   temporary folder, so the script's own folder is not on `sys.path`: a helper
   module placed next to it cannot be imported. Copy shared functions into
   each script.
5. **Return the same table for the same inputs.** Results are cached and
   reused; a script that draws random numbers must seed them, for example
   from `parameters["seed"]`.
6. **`print` for debugging only.** What the script prints is kept only when it
   fails, in its error log.

## Which Python runs the script

The script runs in a separate process, with eqasim's Python interpreter, in
the eqasim folder (`model.root`): pandas, numpy, and geopandas come from
eqasim's environment, not from eqasim-sa's. The interpreter command is, in
order of precedence:

1. `execution.python`, when you write it, for example
   `python: [uv, run, --locked, python]` or the path of a virtual environment's
   interpreter;
2. otherwise `execution.invocation` without its final `-m synpp`, so the
   default invocation gives `uv run --locked python`;
3. without an `execution` block, `uv run --locked python`.

If `execution.invocation` does not end with `-m synpp` and `execution.python`
is absent, loading a meta-configuration that declares analyses fails with
`analyses need execution.python because execution.invocation does not end
with -m synpp`. The command is resolved like the eqasim invocation, including
on Windows. eqasim-sa's part of the process works with Python 3.9 or later.

## What `analyze` does

`analyze` computes only the analyses that at least one indicator reads. For
each one, it copies the script once (editing the script while `analyze` runs
has no effect on that analysis), measures the Python environment once, then
for each run reuses the cached result or runs the script on copies of the
archived inputs. At most `execution.workers` scripts run at the same time
(1 by default).

Any script error makes the whole analysis fail, even with
`completeness_threshold` below 1, with a message naming the analysis, the run,
and the last line of the error:

```text
analysis 'mode_share' failed for run 0e599693c300002e9a1c0dffe06ff0ff: ValueError: trips has no mode column; set mode_choice: true in the eqasim configuration
```

The full error output is in
`<campaign_root>/analyses-cache/logs/<analysis>-<runId>.log`. Fix the script
and run `analyze` again: results already computed are reused.

`check`, including `check --smoke`, does not run analysis scripts: its smoke
runs skip the indicators that read an analysis output. A broken script only
shows up at `analyze`, so try it by hand first.

## Trying a script by hand

Once one run is archived, call `compute` on its files with eqasim's
interpreter. Save this file as `try_commute.py` in the project folder, and
replace `<runId>` with an actual identifier (a folder name under `archives/`):

```python
import importlib.util

archive = "campaigns/mon-analyse-sa-grid/archives/<runId>"
spec = importlib.util.spec_from_file_location("commute", "configs/analyses/commute.py")
commute = importlib.util.module_from_spec(spec)
spec.loader.exec_module(commute)
print(
    commute.compute(
        {
            "trips_spatial": f"{archive}/trips_spatial.gpkg",
            "persons": f"{archive}/persons.csv",
        },
        {"runId": "<runId>", "seed": 1201, "factors": {"sampling_rate": 0.001}},
    )
)
```

```bash
uv run --project ../eqasim-france --locked python try_commute.py
```

Here the script reads the archive itself: it must only read it.

## Cache and recomputation

Each result is identified by a fingerprint: a SHA-256 (domain
`eqasim-sa:analysis-result:v1`) of the analysis name, the script content
(line endings normalized), the SHA-256 of each input file, the run parameters,
and the environment fingerprint. The environment fingerprint is a SHA-256 of
the Python version, the pandas, numpy, and pyarrow versions reported by the
interpreter, and the SHA-256 of `uv.lock` in the eqasim folder. Consequently:

- nothing is recomputed when nothing changed;
- editing a script recomputes the results of that analysis only;
- new runs compute only their own results;
- another Python, pandas, numpy, or pyarrow version, or another `uv.lock`,
  recomputes every result; a change of the eqasim code alone does not (hence
  rule 1).

Results are kept in `<campaign_root>/analyses-cache/<analysis>/<fingerprint>.csv`,
next to `<fingerprint>.sha256`, the SHA-256 of the CSV. A cached entry whose
content no longer matches is removed and recomputed, and the `warnings` field
of the `analyze` result reports it:

```text
analysis cache entry commute/<fingerprint>.csv was altered; it was removed and recomputed
```

The cache can be deleted at any time when no command is running: nothing is
lost, `analyze` recomputes it.

## After a campaign on a cluster

1. Plan and run the campaign on the cluster, with the outputs the future
   scripts may need (see [Before the runs](#before-the-runs-archive-enough)).
2. Copy the campaign folder back to your computer (`scp`, `sftp`, ...). The
   scratch and the data tree are not needed.
3. Render the machine paths of the meta-configuration again for your
   computer, write the scripts, and add `analyses` and the indicators that
   read them.
4. Run `eqasim-sa analyze --meta-config configs/01-grid.yml`.

The computer needs: the campaign folder, the meta-configuration, the scripts,
a data manifest file (the small JSON file, not the data), and the eqasim folder
with its Python environment, which runs the scripts (`uv run --locked`
creates it on first use). The eqasim code, configuration, and `uv.lock` may
differ from those of the runs: `analyze` checks the meta-configuration against
what the plan sealed (`modelDocument` and context), not against the current
eqasim folder. `run`, however, refuses to resume a campaign once eqasim has
changed: finish the runs before changing eqasim. See
[Analyzing on another machine](troubleshooting.md#analyzing-on-another-machine).

## What is recorded

The published analysis records, for every analysis output and every run, the
result fingerprint, the script SHA-256, the environment fingerprint, the
SHA-256 of each input, and the SHA-256 and size of the result:

- in `captured-run-set.json` (schema version 2), under `analyses` in each run;
- in `analysis-generation.json` (version 8), under `provenance.analyses`, by
  analysis then by `runId`.

The definition of an indicator that reads an analysis output describes it by
`{name, analysis, inputs, separator}` (domain
`eqasim-sa:indicator-definition:v4`). See
[Workflow and results](workflow-and-results.md#analysis-scripts).

## Limits

- `inputs` lists outputs declared in `outputs` whose file is a `.csv` or a
  `.gpkg`; an analysis cannot read another analysis.
- A scientific charter's `primary_indicator` cannot read an analysis output:
  indicators computed by scripts are secondary.
- Without an `indicators` block, the default indicators ignore analysis
  outputs: list the indicators you want.
- An analysis name follows the rules of output names and must differ from
  every output name, ignoring case; `logs` is reserved.
