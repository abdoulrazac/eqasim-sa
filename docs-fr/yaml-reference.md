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
| `charter` | optionnelle ; absence = démonstration historique | mapping |
| `execution` | optionnelle | mapping |
| `calibration` | optionnelle | mapping |
| `artifacts` | optionnelle, défaut `{}` | mapping |
| `indicators` | optionnelle, défaut `[]` | liste |

Aucune autre clé racine n'est admise. `campaign` doit correspondre à
`^[A-Za-z0-9][A-Za-z0-9._-]*$`. `campaign_root` ne doit traverser aucun lien
symbolique, doit être extérieur à `model.root`, et son premier ancêtre existant
doit être un répertoire ordinaire.

## `charter`

La charte optionnelle classe les revendications permises par une campagne. Son
absence conserve les anciens YAML comme démonstrations historiques. Une
démonstration explicite a exactement cette forme, sans clé supplémentaire :

```yaml
charter:
  status: demonstration
```

Une campagne de démonstration valide la configuration, l'exécution, le stockage
et la plomberie d'analyse. Elle ne peut soutenir aucune revendication
inférentielle ou décisionnelle. Les plans Grid, ancien `morris`, OAT discret et
Sobol restent lisibles comme provenance de démonstration. L'ancien `morris` sert
uniquement à la compatibilité ; `discrete_oat` est le nom actuel des nouvelles
démonstrations à niveaux adjacents. Aucun des deux n'est Morris canonique.

Une charte scientifique emploie le schéma strict complet suivant :

```yaml
charter:
  status: scientific
  decision:
    claim: Identifier le facteur à apprendre plus précisément en premier
    use: prognostic
    setting: Évaluation des politiques de synthèse de population nantaise
    objective: fp
    estimand: Distance de variation totale espérée sur les seeds de simulation
    primary_indicator: tv_distance(age, ref=INSEE)
  uncertainty:
    dependence:
      kind: declared
      rationale: Deux entrées sont estimées par la même enquête
      groups:
        - factors: [sampling_rate, matching_minimum_age]
          model: Distribution empirique jointe
          provenance: Rapport méthodologique, section 4
    inventory:
      parameters:
        - name: sampling_rate
          status: varied
          provenance: Déclaration du plan
          rationale: Incertitude d'entrée matérielle
        - name: matching_minimum_age
          status: varied
          provenance: Déclaration du plan
          rationale: Incertitude d'entrée matérielle
      data: []
      structures: []
      scenarios: []
      numerics: []
  budget:
    maximum_runs: 1000
    reserve_fraction: 0.1
  stopping:
    criteria:
      - metric: rank_stability
        threshold: 0.05
        consecutive_checks: 3
  robustness:
    checks:
      - name: Modèle de dépendance alternatif
        rationale: Vérifier la conclusion FP sous une dépendance plausible
```

`decision.claim`, `setting`, `estimand` et `primary_indicator` sont des chaînes
non vides. `setting` décrit le contexte décisionnel et `estimand` la quantité à
apprendre. `use` vaut `diagnostic` ou `prognostic`; `objective` vaut criblage
(`screening`), priorisation (`fp`) ou fixation (`ff`). `primary_indicator` doit
être exactement le nom d'un indicateur résolu. Le float fini non négatif
`practical_tolerance` est requis exactement pour `ff` et interdit sinon. Une
configuration scientifique exige au moins un indicateur.

La dépendance est soit `independent`, avec exactement `kind` et un `rationale`
non vide, soit `declared`, avec en plus une liste `groups` non vide. Chaque groupe
a exactement `factors`, `model`, `provenance`, au moins deux noms distincts de
paramètres déclarés, et aucun facteur ne chevauche deux groupes. L'inventaire a
exactement les cinq catégories montrées. Chaque élément a exactement `name`,
`status`, `provenance`, `rationale`; les textes sont non vides, le statut vaut
`varied`, `fixed` ou `excluded`, et les noms sont uniques par catégorie. Les noms
`varied` de l'inventaire des paramètres doivent correspondre exactement aux
paramètres YAML.

`maximum_runs` et chaque `consecutive_checks` sont des entiers signés 64 bits
positifs. `reserve_fraction` est un float fini dans `[0, 1)`. `criteria` est non
vide ; chaque critère contient exactement `metric`, un float fini positif
`threshold`, et `consecutive_checks`, avec des métriques uniques.
`robustness.checks` est non vide. Chaque contrôle contient exactement un `name`
non vide et unique ainsi qu'un `rationale` non vide.

`load_meta_config` valide la structure de la charte. Seul `canonical_morris` peut
actuellement sceller une charte scientifique, et seulement avec
`decision.objective: screening` et une dépendance des paramètres `independent`.
L'ancien `morris`, l'OAT discret et Sobol sont refusés par une charte scientifique.

Les métadonnées du plan canonique conservent un `scientificContract` normalisé.
Sa racine contient exactement `charter` et `parameters`. La charte normalisée
contient exactement `status`, `decision`, `uncertainty`, `budget`, `stopping` et
`robustness`; `stopping` et `robustness` sont directement les listes d'éléments.
Le criblage sérialise `practical_tolerance: null` et l'indépendance
`groups: []`. Les paramètres, triés par nom, contiennent exactement nom, support
et distribution correspondante. Le cœur vérifie toute cette forme et son égalité
avec le design avant publication.

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

### `morris` historique

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

Il s'agit exactement de l'ancien schéma à cinq clés. `trajectories` est un entier
strictement positif, au plus égal au nombre de trajectoires adjacentes candidates.
`design_seed` est un entier signé 64 bits et les seeds de réplication suivent le
contrat Grid. Il est admis uniquement avec une charte absente ou de démonstration
afin de relire une provenance historique. Ne l'utilisez pas pour une nouvelle campagne.

### OAT discret

```yaml
experiment:
  method: discrete_oat
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

La forme et la validation numérique sont les mêmes que pour l'ancien `morris`,
mais l'identité de méthode, les métadonnées de plan et le domaine des `designId`
sont distincts. Une charte de démonstration est obligatoire. Les trajectoires
relient des niveaux numériques adjacents strictement croissants. L'analyse divise
chaque différence de sortie par la taille locale du pas normalisée par l'étendue
totale déclarée. Ce sont des effets normalisés locaux, pas des effets Morris canoniques.

### Morris canonique

```yaml
experiment:
  method: canonical_morris
  seed_role: replication
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
  replications:
    seeds: [1301, 1302]
```

Ce sont exactement les sept clés d'expérience. Morris canonique exige une charte
scientifique d'objectif `screening`, une dépendance `independent` et une
distribution déclarée pour chaque paramètre. `levels` est un entier pair au moins
égal à 4, `trajectories` au moins égal à 2, et `candidate_trajectories` est compris
entre `trajectories` et le minimum de l'univers fini et 500. Un budget déterministe
de calcul des distances s'ajoute au plafond de 500. `design_seed` et les seeds de
base, non vides et uniques, sont des entiers signés 64 bits.

Le pas commun dans l'espace de probabilité unitaire est scellé exactement comme
`Delta = p/[2(p-1)]`, avec `p = levels`. Dans l'ordre lexical des facteurs, un
indice de grille `z` se projette sur un support de longueur `L` par inverse
généralisée uniforme discrète : indice 0 pour `z = 0`, sinon
`min((L*z - 1)//(p-1), L-1)`. Une dérivation SHA-256 versionnée combine
`design_seed`, seed de base, identité de trajectoire et indice de réplication.
Les `D+1` points du bloc partagent la seed dérivée, donc des nombres aléatoires
communs au contraste ; les seeds dérivées sont uniques entre blocs.

Le pool candidat et la sélection sont déterministes. La première paire maximise
la somme des distances quadratiques entre points ; chaque ajout maximise ensuite
la somme de ces distances aux trajectoires retenues, avec départage lexical des
identités. La règle scellée `greedy-sum-pairwise-squared-distance` n'est ni
maximin ni un optimum de Campolongo.

Avant publication, les runs uniques ne dépassent pas
`floor(maximum_runs * (1 - reserve_fraction))`; la réserve reste hors budget du
plan initial. Le plan canonique utilise le schéma v2 et scelle contrat scientifique,
distributions, supports, pool, sélection, Delta, dérivation des seeds et réglages
d'analyse. Le chargement et l'analyse reconstruisent et comparent ce contrat.

`init` ne génère aucun starter Morris canonique : revendication, inventaire
d'incertitude, estimand, budget, convergence, robustesse, supports et provenance
doivent être propres à l'étude. Le fragment suivant illustre seulement le schéma.
Il omet modèle, contexte, artefacts, indicateurs, exécution, calibration et une
partie de l'inventaire ; ce n'est pas une campagne prête à l'emploi :

```yaml
charter:
  status: scientific
  decision:
    claim: <revendication de criblage propre à l'étude>
    use: diagnostic
    setting: <contexte décisionnel déclaré>
    objective: screening
    estimand: <indicateur espéré sur les seeds de simulation>
    primary_indicator: <nom résolu d'indicateur>
  uncertainty:
    dependence:
      kind: independent
      rationale: <justification propre à l'étude>
    inventory:
      parameters:
        - name: sampling_rate
          status: varied
          provenance: <source>
          rationale: <raison de la variation>
      data: []
      structures: []
      scenarios: []
      numerics: []
  budget:
    maximum_runs: 1000
    reserve_fraction: 0.1
  stopping:
    criteria:
      - metric: <métrique déclarée>
        threshold: 0.05
        consecutive_checks: 2
  robustness:
    checks:
      - name: <contrôle déclaré>
        rationale: <raison du contrôle>
experiment:
  method: canonical_morris
  seed_role: replication
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
  replications:
    seeds: [1301, 1302]
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: <source du support propre à l'étude>
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
  convergence_checkpoints: [2, 4, 8]
  screening_top_k: 1
```

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

Avec une charte absente ou de démonstration, chaque entrée exige exactement une
clé `values` :

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

Grid, l'ancien `morris` et l'OAT discret exigent au moins un facteur. À la création ou validation du plan,
chaque domaine Grid doit être non vide et homogène. L'ancien `morris` et l'OAT
discret exigent des domaines
numériques homogènes d'au moins deux niveaux, distincts et strictement
croissants. Morris canonique et Sobol exigent au moins deux scalaires homogènes
distincts par facteur ; le support canonique peut être numérique ou textuel car
la projection passe par la distribution déclarée.

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

Avec `charter.status: scientific`, chaque paramètre, y compris `seed`, exige au
contraire exactement `values` et `distribution` :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Bornes déclarées dans le protocole version 2
```

Pour les domaines actuels sous forme de listes, seuls
`family: discrete_uniform` et `transform: identity` sont admis. `provenance` est
une chaîne non vide. Les familles et transformations analysées sont conservées
comme données typées afin que les futurs échantillonneurs puissent étendre le
vocabulaire. Un support uniforme discret varié contient au moins deux valeurs
distinctes : l'uniformité porte sur les points du support, jamais sur des
positions répétées dans la liste.

## `analysis`

Grid, l'ancien `morris` et l'OAT discret exigent exactement :

```yaml
analysis:
  completeness_threshold: 1.0
```

Le seuil est un `float` YAML fini dans `[0, 1]`; `1` est invalide car il devient
un entier. Les deux méthodes de trajectoires adjacentes imposent `1.0`.

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

Morris canonique exige exactement :

```yaml
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
  convergence_checkpoints: [2, 4, 8]
  screening_top_k: 2
```

Il impose la complétude `1.0` et au moins 100 rééchantillonnages. Les checkpoints
sont des entiers triés, uniques, au moins égaux à 2, et se terminent par
`trajectories`. `screening_top_k` est compris entre 1 et le nombre de facteurs.
Le bootstrap rééchantillonne les trajectoires après moyenne des réplications. Il
fournit une preuve d'incertitude de design fini conditionnelle au contrat scellé,
pas une certification automatique de l'arrêt ou de la robustesse de la charte.

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

L'ancien `morris`, l'OAT discret, Morris canonique et Sobol exigent au moins un
indicateur. Grid accepte une liste vide.
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

Les versions rendues par `init` sont des starters de démonstration :
[Grid](../src/eqasim_sa/templates/01-grid.yml),
[OAT discret](../src/eqasim_sa/templates/02-discrete-oat.yml) et
[Sobol](../src/eqasim_sa/templates/03-sobol.yml).
Aucun starter Morris canonique n'est généré.

### Grid

```yaml
campaign: demo-grid
campaign_root: ../campaigns/grid
charter:
  status: demonstration
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

### OAT discret

```yaml
campaign: demo-discrete-oat
campaign_root: ../campaigns/discrete-oat
charter:
  status: demonstration
model:
  config: ../../eqasim-france/config_local_nantes.yml
  root: ../../eqasim-france
context:
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
analysis:
  completeness_threshold: 1.0
experiment:
  method: discrete_oat
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
charter:
  status: demonstration
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

Domaine Morris historique/OAT discret non ordonné :

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
