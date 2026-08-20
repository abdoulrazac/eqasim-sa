# Référence YAML

[English](../docs/yaml-reference.md) | **Français**

Une méta-configuration décrit une campagne entière. Il n'existe pas de JSON
Schema dans le projet : `src/eqasim_sa/adapters/meta_config.py`, avec les
validateurs appelés, est la norme exécutable.

## Sommaire

- [Sécurité et blocs racine](#sécurité-yaml-globale)
- [Modèle et contexte](#model-et-context)
- [Expériences et paramètres](#experiment)
- [Analyse, artefacts et indicateurs](#analysis)
- [Calibration et exécution](#calibration)
- [Exemples complets](#exemples-complets-valides)
- [Exemples invalides](#exemples-invalides)

## Sécurité YAML globale

Le chargeur est `yaml.SafeLoader`, puis le document doit être compatible JSON.
Les alias YAML, les clés dupliquées, les clés non scalaires et les clés qui ne
se résolvent pas en chaînes sont refusés. Les objets Python, timestamps et autres
types non JSON sont donc interdits. Les nombres non finis sont également refusés.

Tous les chemins relatifs de la méta-configuration sont résolus depuis le
répertoire qui contient ce fichier YAML, pas depuis le répertoire courant.

## Blocs racine

| Clé | Présence | Type |
| --- | --- | --- |
| `campaign` | requise | chaîne |
| `campaign_root` | requise | chaîne chemin |
| `model` | requise | mapping |
| `context` | requise | mapping |
| `experiment` | requise | mapping |
| `parameters` | requise | mapping |
| `analysis` | requise | mapping |
| `execution` | optionnelle | mapping |
| `calibration` | optionnelle | mapping |
| `artifacts` | optionnelle, défaut `{}` | mapping |
| `indicators` | optionnelle, défaut `[]` | liste |

Aucune autre clé racine n'est admise. `campaign` doit correspondre à
`^[A-Za-z0-9][A-Za-z0-9._-]*$`. `campaign_root` ne doit traverser aucun lien
symbolique, doit être extérieur à `model.root`, et son premier ancêtre existant
doit être un répertoire ordinaire.

## `model` et `context`

```yaml
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
```

`model` exige exactement `config` et `root`, deux chaînes. `root` est la racine
exacte d'un dépôt Git. Le dépôt doit être propre. `config` doit être un fichier
ordinaire sous cette racine, suivi par Git et identique au blob de `HEAD`.

`context` exige exactement `data_manifest` et `lockfile`, deux fichiers
ordinaires. Leur contenu est haché en SHA-256. Le chargement ne valide pas la
structure interne du manifeste; `init` la génère et la valide lors d'une relance.

Le `contextHash` dérive de quatre chaînes : commit eqasim, SHA-256 du manifeste,
SHA-256 du lockfile et version installée d'`eqasim-sa`. Sa définition exacte est :

```python
context_descriptor = {
    "eqasimCommit": commit,
    "dataManifestSha256": data_manifest_sha256,
    "lockfileSha256": lockfile_sha256,
    "eqasimSaVersion": version,
}
descriptor = json.dumps(
    context_descriptor,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode()
context_hash = hashlib.sha256(
    b"eqasim-sa:context:v1\0" + descriptor
).hexdigest()
```

Le YAML eqasim chargé par `model.config` doit lui-même être un mapping JSON-safe
avec cette forme minimale :

```yaml
working_directory: /chemin/cache
run: []
config:
  sampling_rate: 0.001
  random_seed: 1
```

`working_directory` est une chaîne, `run` une liste et `config` un mapping. Le
même chargeur sûr interdit alias, clés dupliquées, clés non textuelles et valeurs
non compatibles JSON.

## `experiment`

### Grid

```yaml
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1201, 1202]
```

Les trois clés sont exactes. `seed_role` vaut `replication`. `seeds` est une
liste non vide, sans doublon, d'entiers signés 64 bits. Grid avec seed facteur
n'est pas pris en charge.

### Morris

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

Les cinq clés sont exactes. `trajectories` est un entier strictement positif,
au plus égal au nombre de trajectoires candidates. `design_seed` est un entier
signé 64 bits. Les seeds de réplication suivent le contrat Grid.

### Sobol

```yaml
experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 16
  design_seed: 20260814
```

Les quatre clés sont exactes. `base_sample_size` est une puissance de deux au
moins égale à 2. `design_seed` est un entier non signé 64 bits. La seed est
déclarée dans `parameters.seed`, pas dans `replications`.

## `parameters`

Chaque entrée exige exactement une clé `values` :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
  income_assignation_method:
    values: [uniform, bhepop2]
```

Les valeurs autorisées sont `int`, `float` fini ou chaîne non vide sans NUL.
Elles sont homogènes dans chaque domaine. Les booléens, `null`, listes, mappings,
NaN et infinis sont refusés. Le nom doit être une option synpp sûre, une clé
active de `model.config.config`, un identifiant de facteur valide et non réservé.

Grid et Morris exigent au moins un facteur. À la création ou validation du plan,
chaque domaine Grid doit être non vide et homogène. Morris exige des domaines
numériques homogènes d'au moins deux niveaux, distincts et strictement
croissants. Sobol exige au moins deux valeurs distinctes par facteur.

Pour inspecter les clés candidates et leurs types réels avant d'écrire
`parameters`, depuis la racine du modèle :

```bash
uv run --locked python - <<'PY'
from pathlib import Path
import yaml

document = yaml.safe_load(Path("config_local_nantes.yml").read_text())
for name, value in sorted(document["config"].items()):
    print(f"{name}: {type(value).__name__} = {value!r}")
PY
```

Conservez uniquement une clé scientifique active, non réservée, et des valeurs
du même type que l'option synpp correspondante.

Pour Sobol seulement :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002]
  seed:
    values: [1401, 1402]
```

`seed.values` contient au moins deux entiers signés 64 bits distincts. La seed
est spéciale et n'a pas besoin d'exister dans la configuration modèle.

## `analysis`

Grid et Morris exigent exactement :

```yaml
analysis:
  completeness_threshold: 1.0
```

Le seuil est un `float` YAML fini dans `[0, 1]`; `1` est invalide car il devient
un entier. Morris impose `1.0`.

Sobol exige exactement :

```yaml
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
```

`bootstrap_resamples` est un entier dans `[1, 2^63-1]`.
`confidence_level` est un `float` fini strictement entre 0 et 1. Sobol impose
aussi une complétude de `1.0`.

## `artifacts`

Le bloc associe un nom logique aux colonnes d'une table Parquet :

```yaml
artifacts:
  age:
    class: Age class
    synthetic: Proportion_EQASIM
    references: [INSEE]
```

Chaque artefact exige exactement `class`, `synthetic`, `references`.
`class` et `synthetic` sont des chaînes non vides distinctes. `references` est
une liste éventuellement vide de chaînes non vides, sans doublon, distinctes
des deux autres colonnes. Avec l'adaptateur eqasim, tout artefact référencé par
un indicateur doit être produit par une étape connue : `persons`, `households`,
`activities`, `trips`, `vehicle_types`, `vehicles`, `age`, `age_purpose`,
`distance`, `license`, `nbr_vehicle` ou `travel`.

Avant de déclarer les colonnes, inspectez un Parquet produit par un run connu :

```bash
uv run --project ../eqasim-sa --locked python - <<'PY'
import pyarrow.parquet as pq

path = "campaigns/calibration/archives/<runId>/age.parquet"
parquet = pq.ParquetFile(path)
print(parquet.schema_arrow)
print(parquet.read().column_names)
PY
```

Remplacez `<runId>` par un identifiant réel. Les chaînes `class`, `synthetic` et
`references` doivent reproduire exactement les noms de colonnes affichés.

## `indicators`

Chaque élément contient exactement un type : `pair`, `product` ou `custom`.
Le seul scalariseur intégré est `tv_distance`.

### Paire

```yaml
indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
```

Les trois clés sont requises et sont des chaînes non vides. L'artefact et la
référence doivent avoir été déclarés.

### Produit cartésien

```yaml
indicators:
  - product:
      scalarizers: [tv_distance]
      artifacts: [distance, nbr_vehicle]
```

Les deux listes sont non vides, sans doublon. Le produit crée un indicateur pour
chaque scalariseur, artefact et colonne de référence déclarée par cet artefact.

### Extension personnalisée

```yaml
indicators:
  - custom:
      function: mon_indicateur
      artifact: age
```

Les deux clés sont des chaînes non vides. `function` doit correspondre à
exactement un entry point installé du groupe `eqasim_sa.indicators`. L'objet
chargé doit être appelable ou fournir `compute(artifacts)` et retourner un
`float` fini.

Morris et Sobol exigent au moins un indicateur. Grid accepte une liste vide.
Les noms et identités d'indicateurs résolus doivent être uniques à l'analyse.

### Distance de variation totale

`tv_distance` lit les colonnes classe, synthétique et référence. Les deux
colonnes de masses doivent être numériques, finies, sans valeur nulle,
non négatives et de somme positive. Elles sont normalisées séparément, puis :

`TV = 0.5 × Σ |p_i - q_i|`, avec un résultat borné dans `[0, 1]`.

## `calibration`

```yaml
calibration:
  meta_configs: [00-calibration.yml]
```

Le mapping contient exactement `meta_configs`, liste non vide de chaînes. Les
chemins désignent des fichiers ordinaires, sont dédupliqués, ne peuvent pas se
référencer eux-mêmes et ne peuvent pas contenir à leur tour un bloc
`calibration`. Des chemins supplémentaires peuvent être passés à `check` et
`estimate` par la CLI.

## `execution`

```yaml
execution:
  backend: local
  workers: 2
  scratch: ../scratch
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    data_path: ../../eqasim-data
    processes: 1
```

Les clés requises sont exactement `backend`, `workers`, `scratch`,
`invocation`; `overrides` et `slurm` sont optionnelles. Le bloc entier est
optionnel pour `plan` et `analyze`, mais requis pour `run`, `check` et
`estimate`.

`backend` vaut `local` ou `slurm`. `workers` est un entier strictement positif.
`scratch` est une chaîne non vide sans NUL, sans traversée de lien symbolique,
qui doit déjà désigner un répertoire. `invocation` est une liste non vide de
chaînes non vides sans NUL.

### `overrides`

Le mapping est vide par défaut. Seules les options d'environnement présentes
dans le YAML scellé sont admises : `data_path`, `git_binary`, `java_binary`,
`java_memory`, `maven_binary` et `processes`. La valeur doit conserver exactement
le type du YAML modèle et être transportable comme scalaire synpp; les booléens
ne le sont pas. `data_path` doit résoudre vers un répertoire existant sans
traverser de lien symbolique. Pour SLURM, `processes` doit être un entier
strictement positif.

Les overrides n'entrent pas dans le `runId` : ils décrivent l'infrastructure,
pas un facteur scientifique. Ils sont appliqués seulement à l'exécution.

### `slurm`

```yaml
execution:
  backend: slurm
  workers: 20
  scratch: /scratch/mon-etude
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    processes: 4
  slurm:
    timeout_min: 1440
    mem_gb: 16.0
    partition: compute
    account: equipe-transport
    qos: normal
```

`slurm` est interdit avec le backend local. Toutes ses clés sont optionnelles.
`timeout_min` vaut 1440 par défaut et doit être un entier positif. `mem_gb` est
un entier ou float fini au moins égal à `1/1024`; `null` signifie absent.
`partition`, `account` et `qos` sont `null` ou des identifiants sûrs selon le
même motif que `campaign`. `cpus_per_task` provient de `overrides.processes`, ou
vaut 1.

## Exemples complets valides

Les trois exemples suivants sont complets au niveau du schéma. Leur validation
contextuelle exige avant tout que les chemins existent, que `model.root` soit la
racine exacte d'un dépôt Git propre, que `model.config` soit suivi et identique à
son blob `HEAD`, que le manifeste et le lockfile soient des fichiers ordinaires,
que le scratch existe et que les facteurs/overrides existent dans le YAML modèle
avec les types indiqués. Le fichier `00-calibration.yml` référencé doit également
exister et ne pas contenir de bloc `calibration`.

Les versions rendues par `init` restent les exemples canoniques :
[Grid](../src/eqasim_sa/templates/01-grid.yml),
[Morris](../src/eqasim_sa/templates/02-morris.yml) et
[Sobol](../src/eqasim_sa/templates/03-sobol.yml).

### Grid

```yaml
campaign: demo-grid
campaign_root: ../campaigns/grid
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
analysis:
  completeness_threshold: 1.0
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1201, 1202]
parameters:
  sampling_rate:
    values: [0.001, 0.002]
artifacts:
  age:
    class: Age class
    synthetic: Proportion_EQASIM
    references: [INSEE]
indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
execution:
  backend: local
  workers: 2
  scratch: ../scratch
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    data_path: ../../eqasim-data
    processes: 1
calibration:
  meta_configs: [00-calibration.yml]
```

### Morris

```yaml
campaign: demo-morris
campaign_root: ../campaigns/morris
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
analysis:
  completeness_threshold: 1.0
experiment:
  method: morris
  seed_role: replication
  trajectories: 2
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
artifacts:
  age:
    class: Age class
    synthetic: Proportion_EQASIM
    references: [INSEE]
indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
execution:
  backend: local
  workers: 2
  scratch: ../scratch
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    data_path: ../../eqasim-data
    processes: 1
calibration:
  meta_configs: [00-calibration.yml]
```

### Sobol

```yaml
campaign: demo-sobol
campaign_root: ../campaigns/sobol
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 99
  confidence_level: 0.95
experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 4
  design_seed: 20260814
parameters:
  sampling_rate:
    values: [0.001, 0.002]
  matching_age_scheme:
    values: [matching_6, analysis_7]
  seed:
    values: [1401, 1402]
artifacts:
  age:
    class: Age class
    synthetic: Proportion_EQASIM
    references: [INSEE]
indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
execution:
  backend: local
  workers: 2
  scratch: ../scratch
  invocation: [uv, run, --locked, python, -m, synpp]
  overrides:
    data_path: ../../eqasim-data
    processes: 1
calibration:
  meta_configs: [00-calibration.yml]
```

## Exemples invalides

Float requis écrit comme entier :

```yaml
analysis:
  completeness_threshold: 1
```

Seed simultanément facteur et réplication :

```yaml
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1]
parameters:
  seed:
    values: [1, 2]
```

Domaine Morris non ordonné :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.003, 0.002]
```

Override scientifique ou dédié :

```yaml
execution:
  overrides:
    sampling_rate: 0.002
    output_path: /tmp/output
```

Alias et clé dupliquée interdits :

```yaml
parameters: &params {}
parameters: *params
```
