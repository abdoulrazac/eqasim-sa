# Multi-scramble Sobol: what, when, and how

**Use multi-scramble Sobol only for scientific factor prioritization or factor fixing.**

**English** | [Français](../docs-fr/what-when-how-multiscramble_sobol.md)

## What

Factor prioritization (FP) asks which factor should be learned more accurately
first to reduce expected output uncertainty. Factor fixing (FF) asks whether a
factor may be fixed within a declared practical tolerance, including all
interactions. An interaction is a combined effect of two or more factors.

The method runs several independently randomized Sobol sequences. Each
randomization is an independent scramble. Nested sample sizes reuse exact
prefixes within each scramble. A checkpoint is one of these planned sample
sizes. Evidence is compared across scrambles, not by bootstrapping rows from one
scramble. A bootstrap repeatedly resamples observed units to assess uncertainty.

The finite simulator seed is a non-actionable nuisance factor. Non-actionable
means that it stays diagnostic but cannot receive an FP rank or an FF decision.
A seed is an integer that selects a simulator random stream.

## When

Use it only with a scientific charter, objective `fp` or `ff`, independent
factors, at least four independent scramble seeds, nested sample sizes, a budget,
stopping criteria, and robustness checks. Independent means that knowing one
factor gives no information about another. A stopping criterion is a rule
declared in advance for deciding when evidence is stable enough. A robustness
check tests whether the result survives a declared alternative assumption.

Before using it, write why the factors are independent. For every discrete
factor, write why each label has equal probability. Declare at least four
distinct scramble seeds. Declare sample sizes that double, a budget, a stopping
threshold, and robustness checks. If you cannot justify all of these choices, do
not use this method.

Do not use it for screening. Do not use dependent factors. Named structured
configurations are supported only when equal discrete probabilities and their
provenance are scientifically defensible. Provenance is the written record of
why those probabilities were chosen.

## Parameter examples

The same parameter syntax serves FP and FF; the charter's decision question
changes how results are interpreted. A shared numerical example is:

```yaml
parameters:
  matching_minimum_observations:
    values: [5, 20, 50]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Replace with study evidence supporting these three thresholds
  matching_minimum_age:
    values: [0, 5, 12]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Replace with study evidence supporting these three ages
  seed:
    values: [44011, 44012, 44013, 44014]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Replace with the declared finite simulator-seed rationale
```

For FP, the actionable factors are candidates for learning more accurately. For
FF, they are candidates for fixing within the predeclared total-order tolerance,
including interactions. The finite `seed` support remains non-actionable in both
cases. Never declare ordinary `random_seed` as a factor.

If matching structure is truly part of the decision, this optional factor may be
added to the same `parameters` mapping:

```yaml
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Replace with study evidence for equal probability over these structures
```

Here equal probability applies to the three complete named lists, not to the
attributes inside them. The support, meaning all allowed values or labels, must
be justified; a convenient catalogue is not evidence. Every ordinary factor key
must already exist under `config:` in the sealed model YAML. The special `seed`
factor is the only exception and must not exist there. The base
`matching_attributes` value must already be a list of strings. Follow the
[structured-configurations tutorial](structured-configurations-tutorial.md).

Treat `hts` and `projection_year` primarily as separate data or scenario checks
unless the decision truly models them as uncertain factors. `hts` requires both
`edgt_44` and `entd` datasets and changes donors and references.
The support `[2020, 2030, 2040]` for `projection_year` requires compatible
scenarios and archives. If it is used scientifically, declare it with the same
complete `values` and `distribution` form as the numeric factors above. It
changes the population comparison from the unprojected census to the
projection-reweighted population and its projected INSEE reference. These
domains are syntax examples, not automatic scientific justification. See the
[Nantes parameter catalogue](parameters-and-methods.md#nantes-parameter-catalogue).

## How

The following YAML is a **fragment**. It omits required charter and execution
data and does not run by itself.

```yaml
charter:
  status: scientific
  decision:
    objective: fp
  uncertainty:
    dependence:
      kind: independent
experiment:
  method: multiscramble_sobol
  seed_role: factor
  base_sample_sizes: [8, 16, 32]
  scramble_seeds: [44101, 44102, 44103, 44104]
parameters:
  seed:
    values: [44011, 44012, 44013, 44014]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Declared finite nuisance support
```

The Nantes source files require `render_protocol.py`. Follow the exact
[Nantes rendering instructions](../protocols/nantes-scientific/README.md#rendering).
The gate accepts only a complete, converged, conditional canonical Morris
production screening campaign. Supply that campaign with
`--screening-campaign-root`:

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/03-multiscramble-fp.yml rendered/fp.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/scratch" --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/multiscramble-fp" \
  --screening-campaign-root "$ROOT/campaigns/canonical-morris-production"
```

For FF, use `04-multiscramble-ff.yml`, a different output file, and a different
campaign root. For another study, author a complete configuration. There is no
automatic render step for a generic study.

The project defines `SA` as a Bash command array. `"${SA[@]}"` runs that command
without losing its arguments. See [Getting started](getting-started.md#generated-project-runner).
`plan` creates and seals the plan. `check` validates it and the execution setup.
`estimate` reports the expected resource cost. `run` executes missing runs. `analyze`
creates the result files. The Nantes scientific files have no calibration block.
Therefore, first run `plan`. Then obtain a valid external or calibration resource
estimate, or execute explicitly under your site's procedure. Fresh `check` and
`estimate` calls are not promised to work without resource observations.

```bash
TARGET=rendered/fp.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## What you get

- `indicators.parquet`: one row per completed run and indicator.
- `multiscramble-sobol-indices.parquet`: final aggregate indices and objective ranks.
- `multiscramble-sobol-by-scramble.parquet`: independent estimates by scramble and checkpoint.
- `multiscramble-sobol-convergence.parquet`: between-scramble intervals and checkpoint changes. Such an interval is a range calculated from the independently randomized sequences.
- `multiscramble-sobol-diagnostics.parquet`: range, contrast, and design-reuse diagnostics.
- `output-summary.parquet`, `convergence-evidence.parquet`, `governed-report.json`, and `analysis-generation.json`: summaries, stopping evidence, governed status, paths, and hashes.

The first-order mean is the average estimated share of output variation linked
to one factor alone. In FP, a larger share means that factor should be learned
more accurately first. For FF, the method uses the upper endpoint of its
total-order uncertainty interval. Total order includes all interactions. A
factor is a fixing candidate only when this endpoint is at or below the declared
tolerance for the primary indicator. This endpoint is uncertainty evidence, not
a guaranteed maximum.

Status is `unstable` until all stopping criteria pass, then `conditional`. A
conditional result may be reported provisionally within the declared study. It
is not an authorized robust final claim while checks are pending and
`claimAuthorized` is `false`.

## What you may say

For FP, you may state a conditional learning priority after stopping criteria
pass. For FF, you may state that an actionable factor is conditionally within the
declared tolerance, including interactions, for the sealed primary indicator.

You may not make an authorized or robust claim while robustness is pending. You
may not rank or fix the finite seed nuisance. You may not extend the result to
undeclared supports, dependence, indicators, or scenarios.

## Before you run

- Declare FP or FF, the setting, estimand, and primary indicator. The estimand is the quantity the study intends to estimate.
- For FF, declare the practical tolerance before planning.
- Justify independence and every discrete distribution.
- Use at least four unique scramble seeds and nested doubling sample sizes.
- Check the budget, stopping criteria, and pending robustness checks.
- Keep the finite simulator seed non-actionable.

## Continue

- Previous: [Canonical Morris](what-when-how-canonical_morris.md) and [historical Sobol](what-when-how-sobol.md)
- Next: [Workflow and results](workflow-and-results.md)
- Reference: [YAML](yaml-reference.md#multi-scramble-sobol), [parameters and methods](parameters-and-methods.md#multi-scramble-sobol), and [workflow and results](workflow-and-results.md#multi-scramble-sobol-outputs)
