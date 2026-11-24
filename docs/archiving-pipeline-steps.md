# Archiving the output of another pipeline step

**English** | [Français](../docs-fr/archiving-pipeline-steps.md)

eqasim-sa archives files that eqasim writes in its output folder
(`output_path`) and that you declare under
[`outputs`](yaml-reference.md#outputs). Most eqasim pipeline steps (synpp
stages) do not write there: `synthesis.population.enriched`, for example,
returns the enriched population to the steps that use it, but no file of the
output folder contains it. To keep the result of such a step for each run, add
a small stage of your own that writes it to `output_path`, run it, and declare
its file. eqasim-sa needs nothing else: it does not know stage names and never
reads the synpp cache.

## Contents

- [Where the output of a step lives](#where-the-output-of-a-step-lives)
- [Write an export stage](#write-an-export-stage)
- [Run it](#run-it)
- [Declare its file](#declare-its-file)
- [Consequences](#consequences)

## Where the output of a step lives

A synpp stage has two kinds of results:

- the value returned by its `execute(context)` function (for
  `synthesis.population.enriched`, a pandas table of persons), which synpp
  saves as a pickle file in its cache folder (`working_directory`) and hands
  to the stages that declare it with `context.stage(...)`;
- the files it writes in its own cache folder (`context.path()`), for the
  stages that use them.

Only stages such as `synthesis.output` and `analysis.synthesis.population`
copy results into `output_path`. eqasim-sa does not read the cache instead:
its file names contain hashes computed by synpp from the stage's configuration
and code, reading a pickle file back depends on the versions of Python and of
the libraries that wrote it, and a campaign run on a cluster
brings back only the run archives, not the cache.

## Write an export stage

An export stage is an ordinary synpp stage: a Python module with a
`configure(context)` function that declares what it needs and an
`execute(context)` function that writes the file. This one writes the enriched
population as a Parquet file. Save it as `exports/enriched_population.py`
inside the eqasim folder (`model.root`), next to `synthesis/` and `data/`:

```python
import os


def configure(context):
    context.stage("synthesis.population.enriched")
    context.config("output_path")
    context.config("output_prefix", "ile_de_france_")


def execute(context):
    df_population = context.stage("synthesis.population.enriched")

    output_path = context.config("output_path")
    output_prefix = context.config("output_prefix")

    df_population.to_parquet(
        os.path.join(output_path, output_prefix + "enriched_population.parquet")
    )
```

- `exports` is a plain folder: like eqasim's own `synthesis/` and `data/`
  folders, it needs no `__init__.py`. eqasim-sa starts synpp from the eqasim
  folder (`uv run --locked python -m synpp` by default), and synpp imports the
  stage `exports.enriched_population` from there. Any other folder or module
  name works the same way, as long as it does not clash with an eqasim one.
- `context.stage(...)` names the step whose result you want; replace
  `synthesis.population.enriched` with any other stage that returns a table,
  and change the file name accordingly.
- `ile_de_france_` is the default `output_prefix` of eqasim's
  `synthesis/output.py`; with the same default, your file gets the same prefix
  as eqasim's own files, and `{prefix}` in eqasim-sa designates both.
- Declaring `output_path` with `context.config(...)` is required, not only to
  read it: synpp reuses a cached stage when its configuration has not changed,
  and each run of eqasim-sa has its own `output_path`, so the stage writes its
  file again in every run.

For a CSV file, replace the last instruction with:

```python
    df_population.to_csv(
        os.path.join(output_path, output_prefix + "enriched_population.csv"),
        sep=";", index=False,
    )
```

## Run it

Add the stage to `run:` of your copy of the eqasim configuration
(`model.config`):

```yaml
run:
  - synthesis.output
  - analysis.synthesis.population
  - exports.enriched_population
```

eqasim-sa runs this list as it is (see
[What eqasim runs](yaml-reference.md#what-eqasim-runs)). synpp runs the stage
`synthesis.population.enriched` first if needed; when another stage of the list
already needs it, synpp reuses its result.

## Declare its file

Declare the file under `outputs` with `file`, which is required for a name
eqasim-sa does not know:

```yaml
outputs:
  enriched_population:
    file: "{prefix}enriched_population.parquet"
```

Each run then archives it as `enriched_population.parquet`. An
[analysis script](analysis-scripts.md) can read it with
`pandas.read_parquet`. Indicators (`tv_distance`, `share`, `value`) read CSV
files only: compute the quantity you need with an analysis script, whose
result indicators read, or write the file as CSV and declare its separator:

```yaml
outputs:
  enriched_population:
    file: "{prefix}enriched_population.csv"
    separator: ";"
```

## Consequences

- The stage file is part of the
  [eqasim code fingerprint](yaml-reference.md#the-eqasim-code-fingerprint),
  like every `*.py` file under `model.root`, and the configuration with its
  `run:` list is part of the campaign identity. Add both before `plan`:
  changing them afterwards changes the campaign identity, and every run of a
  planned campaign refuses to start when the code has changed.
- A declared file that the run does not produce (the stage is missing from
  `run:`, or writes another name) makes the run fail with
  `eqasim did not write declared output 'enriched_population' (expected
  nantes_enriched_population.parquet); check that the stage producing it is
  in run: of your eqasim config`. `check --smoke` runs two real runs and
  therefore catches it before the campaign.
- Loading does not check that the stage is in `run:` (that check exists only
  for the known outputs left at their default file), and `show-config` prints
  the declaration with the resolved file name as a comment:

  ```yaml
  outputs:
    enriched_population:
      file: '{prefix}enriched_population.parquet'  # nantes_enriched_population.parquet
  ```

- Every declared file is copied into the archive of every run: a table of
  persons grows with `sampling_rate`.
