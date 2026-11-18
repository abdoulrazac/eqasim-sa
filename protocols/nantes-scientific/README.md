# Nantes Scientific Protocol Ledger

These study-authored files are not `eqasim-sa init` templates. They declare
intended campaigns but make no execution, planning, simulation, or analysis
claim. Every phase has an immutable campaign name and a distinct root.

| Phase | File | Gate | Structural positions | Budget |
| --- | --- | --- | ---: | ---: |
| Canonical Morris pilot | `01-canonical-morris-pilot.yml` | Machine paths | `24 * (2 + 1) * 2 = 144` | 1600 less 10% reserve = 1440 |
| Canonical Morris production | `02-canonical-morris-production.yml` | Valid pilot root with a recommended allocation | `24 * (2 + 1) * R`; at `R <= 20`, at most 1440 | 1600 less 10% reserve = 1440 |
| Factor prioritization | `03-multiscramble-fp.yml` | Complete, converged conditional screening root | `4 * 32 * (2 * 3 + 2) = 1024` | 1280 less 20% reserve = 1024 |
| Factor fixing | `04-multiscramble-ff.yml` | Complete, converged conditional screening root | `4 * 64 * (2 * 3 + 2) = 2048` | 2560 less 20% reserve = 2048 |

The pilot and production use the same 24-trajectory geometry. The pilot's two
replications provide the variance evidence used to choose only `R` in `[2, 20]`;
its `replication-allocation.json` does not provide production seeds. The
preregistered production seed pool is `[45001, ..., 45020]`, and production uses
exactly its first `R` seeds.

Both numerical factors, `matching_minimum_observations` and
`matching_minimum_age`, are conservatively retained in FP and FF because
screening cannot justify fixing a factor. The third Sobol dimension is the
finite simulator-seed nuisance. FP and FF are blocked until a valid production
screening root supplies complete, converged conditional evidence for both
retained factors.

## Platforms

The protocol is rendered and run the same way on Windows, macOS, and Linux. The
command examples below are Bash; on Windows, run them from Git Bash. On
Windows, `execution.scratch` must be very short (for example `C:\esa`), and
because eqasim's own cache creates relative paths of up to 231 characters,
Nantes cannot fit in 260 characters unless Windows long paths
(`LongPathsEnabled`) are enabled by an administrator once; `check` fails early
with an explanation otherwise. A campaign runs on a single machine; render the
machine paths again on each machine that runs or analyzes it. See
[Troubleshooting](../../docs/troubleshooting.md#path_length-check-fails-on-windows).

## Rendering

`render_protocol.py` is the only documented rendering path. It structurally
loads and dumps YAML, requires every machine path, and refuses to overwrite an
existing output. Use an absolute, phase-specific `--campaign-root`:

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/01-canonical-morris-pilot.yml rendered/pilot.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/canonical-morris-pilot"
```

For production, add the pilot campaign root; the renderer reads and hashes the
allocation itself and derives `R` and the first `R` preregistered seeds:

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/02-canonical-morris-production.yml rendered/production.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" --scratch "$ROOT/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/canonical-morris-production" \
  --pilot-campaign-root "$ROOT/campaigns/canonical-morris-pilot"
```

For FP, use the production screening campaign as evidence; FF uses the same
command with template `04-multiscramble-ff.yml` and its own output/root:

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/03-multiscramble-fp.yml rendered/fp.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" --scratch "$ROOT/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/multiscramble-fp" \
  --screening-campaign-root "$ROOT/campaigns/canonical-morris-production"
```

The renderer verifies the manifest path and SHA-256 rather than accepting a
caller-supplied digest. Each upstream root must contain a regular, non-symlink
`plan.parquet` whose canonical Morris design and context descriptor match this
protocol and the current model, manifest, lockfile, and `eqasim-sa` version. It
rejects symlink traversal, malformed evidence, capped or infeasible allocations,
out-of-range `R`, incomplete or unconverged screening, unresolved markers, and
model configurations that contradict these exact fixed values:
`sampling_rate: 0.001`, `matching_age_scheme: matching_6`,
`analysis_age_scheme: analysis_7`, `income_assignation_method: uniform`,
`hts: edgt_44`, and `departments: [44]`.

The FP/FF gate deliberately permits a **conditional follow-up analysis**, not a
final claim. It requires `claimAuthorized: false`, pending robustness with a
nonempty set of pending checks, and the `robustness_pending` reason code. A later
governed decision still requires robustness and claim authorization.

After rendering, `eqasim-sa check` and `eqasim-sa plan` remain explicit future
operations; rendering itself satisfies no stopping criterion and claims no
execution. Human review must link declared robustness evidence before any
governed report can authorize a claim.

## Source Provenance

Concrete eqasim-france source identifiers for this protocol are:

- Model configuration: `eqasim-france/config_local_nantes.yml`
- Matched population: `eqasim-france/synthesis/population/matched.py`
- Analysis outputs: `eqasim-france/analysis/synthesis/population.py`

The generated plan context seals the eqasim-france source fingerprint and data
manifest digest; these path identifiers do not replace those immutable seals.

## Protocol Role Inventory

The protocol-level roles below are more expressive than the YAML inventory,
whose schema can encode only `varied`, `fixed`, or `excluded`. In YAML,
stochastic integration is represented through `charter.stochastic`, while
robustness-only candidates appear as fixed or excluded plus an explicit
robustness check.

| Candidate | Protocol role | Concrete source path | Rationale |
| --- | --- | --- | --- |
| `matching_minimum_observations` | varied | `eqasim-france/config_local_nantes.yml`; `eqasim-france/synthesis/population/matched.py` | Actionable numerical threshold screened on `[5, 20, 50]` and retained for FP/FF. |
| `matching_minimum_age` | varied | `eqasim-france/config_local_nantes.yml`; `eqasim-france/synthesis/population/matched.py` | Actionable numerical threshold screened on `[0, 5, 12]` and retained for FP/FF. |
| `sampling_rate` | fixed | `eqasim-france/config_local_nantes.yml` | Sealed at `0.001`; alternative rates are numerical robustness checks, not attribution factors. |
| Simulator `random_seed` / `seed` | stochastic | `eqasim-france/config_local_nantes.yml`; `charter.stochastic` and `experiment.seeds` in these protocol files | Morris integrates a conditional mean with CRN; Sobol uses a declared finite non-actionable nuisance support. |
| `processes` / process count | robustness-only | `eqasim-france/config_local_nantes.yml`; `execution.machine_options.processes` in these protocol files | Sealed to one for campaigns; separate identical-seed runs must test process-count determinism. |
| `analysis_age_scheme` | fixed | `eqasim-france/config_local_nantes.yml`; `eqasim-france/analysis/synthesis/population.py` | `analysis_7` defines the primary output and must remain comparable across the main campaigns. |
| `matching_age_scheme` | robustness-only | `eqasim-france/config_local_nantes.yml`; `eqasim-france/synthesis/population/matched.py` | `matching_6` is sealed; alternatives change matching structure and require separate checks. |
| `income_assignation_method` | robustness-only | `eqasim-france/config_local_nantes.yml`; `eqasim-france/synthesis/population/income/uniform.py` | Uniform assignment is sealed; alternative income models are structural checks. |
| INSEE RP 2022 age reference | fixed | `eqasim-france/data/census/`; `eqasim-france/analysis/reference/census/sociodemographics.py` | Declared primary age reference for Loire-Atlantique; other years or sources are data-reference robustness. |
| `hts: edgt_44` and EDGT data | fixed | `eqasim-france/config_local_nantes.yml`; `eqasim-france/data/hts/edgt_44/` | Declared secondary distance and vehicle-count reference; alternative surveys are robustness-only. |
| Population projection model | robustness-only | `eqasim-france/synthesis/population/projection/reweighted.py`; `eqasim-france/synthesis/population/projection/ipu.py` | Projection alternatives alter model structure and are not mixed into primary attribution. |
| Home-location model | robustness-only | `eqasim-france/synthesis/population/spatial/home/locations.py`; `eqasim-france/synthesis/locations/output/home.py` | Location alternatives are structural checks requiring separate campaigns. |
| Education-location model | robustness-only | `eqasim-france/synthesis/locations/education.py`; `eqasim-france/synthesis/locations/output/education.py` | Education assignment alternatives are structural checks, not numerical factors. |
| Vehicle-generation model | robustness-only | `eqasim-france/synthesis/vehicles/vehicles.py`; `eqasim-france/synthesis/vehicles/cars/default.py` | Vehicle model alternatives are checked separately from matching-threshold attribution. |
| `mode_choice` and policy scenario | excluded | `eqasim-france/config_local_nantes.yml`; `eqasim-france/matsim/` | Mode choice is disabled in the sealed synthesis baseline; policy or mode-choice scenarios require distinct confirmation campaigns. |
| Synthetic/HTS distance definition | robustness-only | `eqasim-france/analysis/synthesis/mode_distances.py`; `eqasim-france/analysis/reference/hts/mode_distances.py` | Synthetic Euclidean and survey routed distances are asymmetric and must be disclosed and checked separately. |
| Department/region geography | fixed | `departments`, `regions`, and `iris_path` in `eqasim-france/config_local_nantes.yml` | Department 44 and its declared IRIS geography define the decision setting. |
| External geography/data paths | fixed | `data_path`, `gtfs_path`, `osm_path`, `ban_path`, `bdtopo_path`, `bpe_path`, and `iris_path` in `eqasim-france/config_local_nantes.yml`; rendered `model.data_manifest` | Inputs are machine-specific but sealed by the data-manifest digest; changing them creates a different evidence context. |

## Scientific Contract

The primary quantity is total-variation distance for `analysis_7` age classes
against INSEE RP 2022. Distance and vehicle-count TV against EDGT 44 are
secondary. Distance reporting must disclose synthetic Euclidean distance versus
survey routed-distance asymmetry.

Canonical Morris targets the conditional mean over simulator seeds and uses CRN
within each trajectory. Multi-scramble Sobol treats the declared finite seed
support as a non-actionable nuisance; it does not identify unrestricted
intrinsic simulator variance. FF uses total-order upper confidence bounds and a
provisional variance-share tolerance of `0.05`, including interactions.

The inventory records parameter, data, structural, scenario, and numerical
uncertainties. Structural, data-source, process-determinism, factor-support,
age-scheme, distance-definition, and scenario checks remain outside the primary
attribution design.
