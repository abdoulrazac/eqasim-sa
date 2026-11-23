# Référence YAML

[English](../docs/yaml-reference.md) | **Français**

Une méta-configuration est l'unique fichier YAML qui décrit une expérience (une
campagne) : quels paramètres du modèle varient, quelle méthode les explore, et
quelles sorties du modèle sont comparées à quelles données de référence. Une
expérience correspond à un fichier ; plusieurs expériences, à plusieurs
fichiers.

Vous n'écrivez que ce qui compte pour votre étude. Tout ce qui n'a qu'une valeur
raisonnable, ou qu'`init` connaît déjà, a une valeur par défaut. La commande
`eqasim-sa show-config FICHIER` affiche le fichier complet, toutes valeurs par
défaut remplies (voir [Valeurs par défaut et `show-config`](#valeurs-par-défaut-et-show-config)).

Il n'existe pas de JSON Schema dans le projet :
`src/eqasim_sa/adapters/meta_config.py`, avec les validateurs qu'il appelle, est
la norme exécutable, et `src/eqasim_sa/adapters/meta_config_defaults.py`
contient toutes les valeurs par défaut.

## Sommaire

- [Un premier fichier](#un-premier-fichier)
- [Valeurs par défaut et `show-config`](#valeurs-par-défaut-et-show-config)
- [Règles générales](#règles-générales)
- [Clés racine](#clés-racine)
- [`model`](#model)
- [`experiment`](#experiment)
- [`parameters`](#parameters)
- [`analysis`](#analysis)
- [`outputs`](#outputs)
- [`analyses`](#analyses)
- [`indicators`](#indicators)
- [`execution`](#execution)
- [`calibration`](#calibration)
- [`charter`](#charter)
- [Exemples complets valides](#exemples-complets-valides)
- [Exemples invalides](#exemples-invalides)

## Un premier fichier

Voici le fichier de départ Grid qu'`init` écrit dans `configs/01-grid.yml`, ici pour un
projet nommé `mon-analyse-sa` :

```yaml
# Two rates by two seeds: four positions and four unique runs.
campaign: mon-analyse-sa-grid
experiment:
  method: grid
  seeds: [1201, 1202]
parameters:
  sampling_rate: [0.001, 0.002]
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
  distance:
    category: Distance class
    simulated: Proportion_EQASIM
    references: [HTS]
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references: [HTS]
calibration:
  meta_configs: [00-calibration.yml]
# --- Machine-specific values ---
# eqasim-sa init fills in campaign (above), the paths below and, when given,
# the eqasim command, and compares them when it is rerun: edit them only when
# the machine changes. workers and machine_options.processes are yours:
# change them freely.
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

Le fichier généré contient aussi quelques lignes de commentaire (omises ici)
qui rappellent le prérequis sur votre configuration eqasim et montrent comment
ajouter un second paramètre.

Lisez-le de haut en bas :

- `campaign` nomme l'expérience. Ses résultats vont dans
  `../campaigns/mon-analyse-sa-grid`.
- `experiment` choisit la méthode (`grid` essaie toutes les combinaisons) et
  les graines de simulation (*seeds*). Chaque combinaison est exécutée une fois
  par graine.
- `parameters` donne, pour chaque paramètre eqasim que vous faites varier, les
  valeurs à essayer. Le fichier de départ ne fait varier que `sampling_rate`, que la
  configuration officielle d'eqasim contient déjà.
- `outputs` liste les fichiers de chaque run qu'eqasim-sa conserve. Une sortie
  eqasim connue comme `persons` n'a besoin de rien d'autre (`{}`) ;
  `activities_spatial` n'est pas un nom connu, son fichier est donc écrit
  explicitement. Pour les trois distributions `age`, `distance` et
  `nbr_vehicle`, le bloc indique aussi la colonne des classes (`category`), la
  colonne des proportions simulées (`simulated`) et les colonnes de référence
  auxquelles les comparer (`references`). Sans bloc `indicators`, la distance
  de variation totale est calculée pour chaque sortie et chacune de ses
  références.
- `calibration` désigne le fichier de calibration des ressources utilisé par
  `check` et `estimate`.
- La partie sous le commentaire « Machine-specific values » contient les
  chemins du dossier eqasim, de son fichier de configuration, des données et
  du répertoire de travail temporaire sur cette machine. `init` les remplit
  (avec `campaign`) et une relance d'`init` les compare : ne les modifiez que
  si la machine change ; `workers` et `machine_options.processes` sont à vous
  et se modifient librement. `config: config.yml` est lu depuis le dossier
  eqasim (`model.root`).

Avant toute planification, ouvrez votre copie de la configuration eqasim et
vérifiez que sa liste `run:` contient `analysis.synthesis.population` : les
fichiers `age`, `distance` et `nbr_vehicle` sont écrits par cette étape
d'eqasim, et le `config.yml` officiel ne liste que `synthesis.output`. Voir
[Ce qu'eqasim exécute](#ce-queqasim-exécute).

## Valeurs par défaut et `show-config`

Quand une clé est omise, eqasim-sa applique les valeurs par défaut suivantes.

| Clé | Valeur par défaut |
| --- | --- |
| `campaign_root` | `../campaigns/<campaign>` |
| `model.lockfile` | `<model.root>/uv.lock` |
| `experiment.design_seed` (OAT discret, Morris canonique, Sobol) | dérivée du nom de campagne (voir ci-dessous) |
| `parameters.NOM: [a, b]` | identique à `parameters.NOM: {values: [a, b]}` |
| `analysis` | un bloc vide, complété comme ci-dessous |
| `analysis.completeness_threshold` | `1.0` (toutes les méthodes) |
| `analysis.bootstrap_resamples` | `999` (Sobol, Morris canonique) |
| `analysis.confidence_level` | `0.95` (Sobol, Morris canonique, Sobol multi-scramble) |
| `outputs.NOM.file`, `outputs.NOM.separator` (sortie connue) | le fichier et le séparateur eqasim indiqués dans [Sorties connues](#sorties-connues-et-leurs-fichiers) |
| `outputs.NOM.separator` (autre sortie) | `,` |
| `outputs.NOM.references` | une liste vide |
| `indicators` | une distance de variation totale pour chaque sortie qui a au moins une référence |
| `execution.backend` | `local` (seulement si un bloc `execution` est présent) |
| `execution.workers` | `1` (seulement si un bloc `execution` est présent) |
| `execution.invocation` | `[uv, run, --locked, python, -m, synpp]` (seulement si un bloc `execution` est présent) |
| `execution.python` | `execution.invocation` privée de son `-m synpp` final, ou `[uv, run, --locked, python]` sans bloc `execution`. `show-config` l'écrit quand un bloc `execution` est présent et que `invocation` se termine par `-m synpp` ; il n'écrit rien sans bloc `execution` (en écrire un exigerait `scratch`) ni quand `invocation` ne se termine pas par `-m synpp` (aucun interpréteur n'est alors déduit, et un fichier qui déclare `analyses` doit écrire `python`) |
| `analyses.NOM.references` | une liste vide ; `show-config` ne l'écrit pas |

Les chemins sont relatifs au répertoire du fichier YAML : la racine de campagne
par défaut se trouve donc à côté du répertoire `configs/`. Les chemins par
défaut utilisent `/`, valable sous Windows, macOS et Linux.

La `design_seed` par défaut est calculée à partir du nom de campagne : les huit
premiers octets de `SHA-256(b"eqasim-sa:design-seed:v1\0" + UTF8(campaign))`,
lus comme un entier gros-boutiste puis décalés d'un bit vers la droite. Elle est
donc toujours dans `[0, 2^63)`, ce qui convient à toutes les méthodes. Renommer
une campagne change sa `design_seed` par défaut, donc son design ; écrivez
`design_seed` explicitement pour conserver le design sous un autre nom.

Les valeurs par défaut ne changent jamais en silence sous une campagne en cours.
Le fichier résolu ne dépend que des octets de votre fichier YAML (scellés dans
`metaConfigSha256`) et de la version d'eqasim-sa (incluse dans le `contextHash`,
qui refuse déjà de reprendre une campagne avec une autre version). Rien de plus
n'est écrit sur le disque.

Pour voir le fichier complet qu'eqasim-sa utilise réellement :

```bash
"${SA[@]}" show-config configs/01-grid.yml
```

La commande valide le fichier comme le fait `plan` (dossier eqasim et
configuration trouvés, facteurs présents dans la configuration eqasim), puis affiche le YAML complet
sur la sortie standard, toutes valeurs par défaut appliquées. Les chemins sont
affichés tels que vous les avez écrits : la sortie est elle-même une
méta-configuration valide. Si le fichier est invalide, le message d'erreur part
sur la sortie d'erreur et le code de sortie vaut 1 ; contrairement aux autres
sous-commandes, `show-config` n'affiche pas d'enveloppe JSON. Pour le fichier de départ
ci-dessus, la sortie figure dans
[Exemples complets valides](#le-même-grid-avec-toutes-les-valeurs-par-défaut-écrites).

## Règles générales

Le chargeur est `yaml.SafeLoader`, puis le document doit être compatible JSON.
Les alias YAML, les clés dupliquées, les clés non scalaires et les clés qui ne
se résolvent pas en chaînes sont refusés. Les objets Python, timestamps et autres
types non JSON sont donc interdits. Les nombres non finis sont également refusés.

Tous les chemins relatifs de la méta-configuration sont résolus depuis le
répertoire qui contient ce fichier YAML, pas depuis le répertoire courant.

Chaque bloc n'accepte que ses propres clés. Une clé mal orthographiée ou
inconnue est une erreur, et le message liste les clés permises à ce niveau.

## Clés racine

| Clé | Présence | Contenu |
| --- | --- | --- |
| `campaign` | requise | nom de l'expérience |
| `campaign_root` | facultative | dossier de l'état et des résultats de la campagne |
| `model` | requise | le modèle eqasim et ses entrées scellées |
| `experiment` | requise | méthode et graines |
| `parameters` | requise | paramètres eqasim que l'on fait varier, et leurs valeurs |
| `analysis` | facultative | réglages de l'analyse |
| `outputs` | requise | fichiers conservés de chaque run, et colonnes que les indicateurs y lisent |
| `analyses` | facultative | scripts qui calculent des sorties d'analyse à partir des fichiers archivés |
| `indicators` | facultative | grandeurs calculées à partir des sorties |
| `execution` | facultative pour `plan` et `analyze` ; requise pour `run`, `check` et `estimate` | façon d'exécuter les runs sur cette machine |
| `calibration` | facultative | fichiers de calibration des ressources |
| `charter` | facultative ; absence = démonstration | ce que la campagne peut revendiquer |

Aucune autre clé racine n'est admise. `campaign` doit correspondre à
`^[A-Za-z0-9][A-Za-z0-9._-]*$` et compter au plus 64 caractères ; les noms
réservés de Windows et les noms qui ne diffèrent que par la casse sont refusés
sur tous les systèmes. `campaign_root` ne doit traverser aucun lien symbolique,
doit être extérieur à `model.root`, et son premier ancêtre existant doit être un
répertoire ordinaire. Utilisez un nouveau `campaign` pour tout design modifié :
avec le `campaign_root` par défaut, la campagne reçoit aussi un nouveau dossier.

## `model`

```yaml
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
```

`root`, `config` et `data_manifest` sont requises ; `lockfile` est facultative
et vaut par défaut `<root>/uv.lock`. Ce sont quatre chaînes de chemin.

`root` est le dossier eqasim : un clone, une copie ou une archive décompressée
fonctionnent de la même façon. Git n'est pas nécessaire, et eqasim-sa ne
l'appelle jamais. `root` doit être un répertoire existant qui n'est pas un lien
symbolique.

`config` est le fichier de configuration eqasim de votre étude. Un chemin
relatif est lu depuis `root`, exactement comme quand vous lancez eqasim
vous-même depuis son dossier : `config: config.yml` désigne le fichier
officiel, et `config: my_config.yml` une copie que vous avez faite à côté (par
exemple avec `cp config.yml my_config.yml`, ou
`cp config_nantes.yml config_nantes_sa.yml` pour Nantes, puisque eqasim fournit
lui-même `config_nantes.yml`).
Un chemin absolu est aussi accepté : le fichier peut alors se trouver hors du
dossier eqasim. Il doit s'agir d'un fichier ordinaire ; il n'a pas besoin
d'être sous gestion de versions. Son contenu fait partie de l'identité de la
campagne.

`data_manifest` et `lockfile` sont des fichiers ordinaires dont le contenu est
haché en SHA-256. Le chargement ne valide pas la structure interne du
manifeste ; `init` le génère et le valide lors d'une relance.

### L'empreinte du code d'eqasim

eqasim-sa identifie le code d'eqasim par une empreinte de son contenu : un
SHA-256 calculé sur tous les fichiers `*.py` et `*.java` et tous les
`pom.xml`, `pyproject.toml` et `uv.lock` situés sous `root`. Sont laissés de
côté :

- les dossiers dont le nom commence par un point (par exemple `.venv`) et les
  dossiers `__pycache__` ;
- les environnements virtuels Python et les environnements conda (tout dossier
  qui contient `pyvenv.cfg` ou `conda-meta`) : installer des paquets ne change
  donc jamais l'empreinte ;
- les dossiers `working_directory` (cache) et `config.output_path` de la
  configuration eqasim, quand ils se trouvent sous `root` ;
- les liens symboliques et les jonctions Windows, ni suivis ni hachés.

Le contenu des fichiers est haché avec des fins de ligne normalisées en LF, et
les chemins sont triés, écrits avec `/` et normalisés en Unicode NFC : un même
code donne la même empreinte sous Windows, macOS et Linux. Toute modification
du code d'eqasim change l'empreinte, donc l'identité de la campagne. Pendant
une campagne, chaque run recalcule l'empreinte et refuse de démarrer si le code
a changé.

Le `contextHash` dérive de quatre chaînes : l'empreinte du code d'eqasim, le
SHA-256 du manifeste, le SHA-256 du lockfile et la version installée
d'`eqasim-sa`. Sa définition exacte est :

```python
context_descriptor = {
    "eqasimSourceSha256": eqasim_source_sha256,
    "dataManifestSha256": data_manifest_sha256,
    "lockfileSha256": lockfile_sha256,
    "eqasimSaVersion": version,
}
descriptor = json.dumps(
    context_descriptor,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
    allow_nan=False,
).encode("ascii")
context_hash = hashlib.sha256(
    b"eqasim-sa:context:v3\0" + descriptor
).hexdigest()
```

### La configuration eqasim

Le YAML eqasim chargé par `model.config` doit lui-même être un mapping
compatible JSON contenant au minimum la structure suivante :

```yaml
working_directory: /chemin/cache
run:
  - synthesis.output
  - analysis.synthesis.population
config:
  sampling_rate: 0.001
  random_seed: 1
```

`working_directory` est une chaîne, `run` une liste et `config` un mapping. Le
même chargeur sûr interdit alias, clés dupliquées, clés non textuelles et valeurs
non compatibles JSON.

### Ce qu'eqasim exécute

eqasim-sa exécute la liste `run:` de votre configuration eqasim telle quelle :
il n'ajoute, ne retire ni ne réordonne aucune étape. Chaque run ne change que
les paramètres que l'on fait varier, la graine (`random_seed`), le dossier de
cache (`working_directory`) et le dossier de sortie (`output_path`), qui
pointent vers l'espace temporaire propre au run.

La liste `run:` décide donc des fichiers présents à la fin d'un run. Le
`config.yml` officiel ne liste que `synthesis.output`, qui écrit les fichiers
de population (`persons`, `households`, `trips`, `activities`…). Les
distributions comparées aux données du recensement et des enquêtes (`age`,
`distance`, `nbr_vehicle`…) sont écrites par l'étape eqasim
`analysis.synthesis.population` ; ajoutez-la à `run:` dans votre copie de la
configuration :

```yaml
run:
  - synthesis.output
  - analysis.synthesis.population
```

Quand une sortie connue garde son fichier par défaut (voir
[`outputs`](#outputs)) et que l'étape qui l'écrit manque dans `run:`, le
chargement s'arrête avec un message comme :

> output 'age' is written by eqasim stage 'analysis.synthesis.population',
> which is not in run: of /chemin/eqasim-france/config.yml; add it to run: in
> your eqasim config

Le message donne le chemin complet de la configuration lue.

## `experiment`

`method` choisit le design et `seeds` liste les graines de simulation. Les
autres clés dépendent de la méthode :

| Méthode | Clés permises | Charte |
| --- | --- | --- |
| `grid` | `method`, `seeds` | démonstration ou scientifique |
| `discrete_oat` | `method`, `seeds`, `trajectories`, `design_seed` | démonstration |
| `canonical_morris` | `method`, `seeds`, `levels`, `trajectories`, `candidate_trajectories`, `design_seed` | scientifique |
| `sobol` | `method`, `seeds`, `base_sample_size`, `design_seed` | démonstration |
| `multiscramble_sobol` | `method`, `seeds`, `base_sample_sizes`, `scramble_seeds`, `seeds_provenance` | scientifique |

`design_seed` est facultative ; sa valeur par défaut est dérivée du nom de
campagne (voir [Valeurs par défaut](#valeurs-par-défaut-et-show-config)). Toutes
les autres clés du tableau sont requises.

### Graines

`experiment.seeds` est le seul endroit où s'écrivent les graines de simulation.
C'est une liste non vide, sans doublon, d'entiers signés 64 bits. Son sens
dépend de la méthode :

- Pour Grid, OAT discret et Morris canonique, les graines sont des
  **réplications** : chaque point du design est exécuté une fois par graine, et
  l'analyse en fait la moyenne ou en rapporte la dispersion.
- Pour Sobol et Sobol multi-scramble, les graines forment le support d'un
  **facteur** supplémentaire, nommé `seed`, qui entre dans la décomposition de
  variance. Il faut alors au moins deux graines. Les inventaires de charte et
  les résultats d'indicateurs appellent ce facteur `seed`.

Déclarer un paramètre nommé `seed` sous `parameters` est refusé : les graines
s'écrivent toujours dans `experiment.seeds`.

### Grid

```yaml
experiment:
  method: grid
  seeds: [1201, 1202]
```

Grid exécute chaque combinaison des valeurs de paramètres déclarées une fois par
graine.

### OAT discret

```yaml
experiment:
  method: discrete_oat
  seeds: [1301, 1302]
  trajectories: 8
```

`trajectories` est un entier strictement positif au plus égal au nombre de
candidats à niveaux adjacents. `design_seed`, si elle est écrite, est un entier
signé 64 bits. La méthode exige une charte de démonstration. Ses trajectoires
passent entre niveaux numériques adjacents et strictement croissants ; l'analyse
divise chaque différence de sortie par ce pas local normalisé par l'étendue
complète déclarée du facteur. Ce sont des effets locaux normalisés, pas des
effets de Morris canonique.

### Morris canonique

```yaml
experiment:
  method: canonical_morris
  seeds: [1301, 1302]
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
```

Morris canonique exige une charte scientifique avec l'objectif `screening`, une
dépendance indépendante des paramètres et une distribution déclarée pour chaque
paramètre. `levels` est un entier pair d'au moins 4 ; `trajectories` vaut au
moins 2 ; `candidate_trajectories` est strictement supérieur à `trajectories`,
au plus égal à l'univers fini des candidats, et au plus 500. Un budget
déterministe de complexité des calculs de distance s'ajoute à ce plafond de 500.
`design_seed` et chaque graine sont des entiers signés 64 bits.

Le pas commun en probabilité unitaire est conservé exactement sous la forme
rationnelle `Delta = p/[2(p-1)]`, où `p = levels`. Les valeurs sont projetées
dans l'ordre lexicographique des facteurs par l'inverse généralisé de la loi
uniforme discrète déclarée. Pour un support de longueur `L` et un indice de
grille `z`, l'indice de support vaut 0 en `z = 0`, sinon
`min((L*z - 1)//(p-1), L-1)`. Une dérivation SHA-256 versionnée combine
`design_seed`, la graine de base, l'identité de la trajectoire sélectionnée et
l'indice de réplication. Les `D+1` points d'un même bloc trajectoire-réplication
partagent la graine dérivée, ce qui fournit des nombres aléatoires communs dans
chaque contraste d'effet élémentaire ; les graines dérivées doivent être uniques
entre blocs.

L'échantillon candidat et les trajectoires sélectionnées sont déterministes pour
la graine scellée. SALib 1.5.2 effectue l'appel Morris public avec optimisation
locale et `optimal_trajectories: trajectories`. Les trajectoires candidates sont
tirées avec remise : une même matrice peut donc être sélectionnée plusieurs fois
sur une grille finie. Chaque occurrence a un indice sélectionné et une graine de
simulation dérivée distincts ; toutes les matrices sélectionnées et les options
SALib effectives sont scellées dans le design v3.

Avant de publier un plan Morris canonique ou Sobol multi-scramble, les positions
structurelles du design, sans déduplication par `runId`, ne doivent pas dépasser
`floor(maximum_runs * (1 - reserve_fraction))`. La fraction réservée reste hors
du budget d'exécution initial. Les plans canoniques utilisent le schéma de plan
v2 et scellent le contrat scientifique normalisé complet, les distributions, les
supports, les identités des candidats, les trajectoires sélectionnées, Delta, la
dérivation des graines et les réglages d'analyse. Le chargement et l'analyse
reconstruisent et comparent ce contrat.

`init` ne génère pas de fichier de départ Morris canonique, car la revendication
scientifique, l'inventaire des incertitudes, l'estimande, le budget, les
critères de convergence, les contrôles de robustesse, les supports et la
provenance doivent être propres à l'étude. Le fragment suivant n'est qu'une
illustration ; il omet volontairement `model`, `outputs`, `execution`,
`calibration` et la plupart des entrées d'inventaire, et n'est donc pas une
campagne prête. Le [protocole scientifique de Nantes](../protocols/nantes-scientific/README.md)
contient des fichiers scientifiques complets.

```yaml
charter:
  status: scientific
  decision:
    claim: <study-specific screening claim>
    use: diagnostic
    setting: <declared decision setting>
    objective: screening
    estimand: <expected indicator over simulation seeds>
    primary_indicator: <resolved indicator name>
  uncertainty:
    dependence:
      kind: independent
      rationale: <study-specific justification>
    inventory:
      parameters:
        - name: sampling_rate
          status: varied
          provenance: <source>
          rationale: <why varied>
        - name: matching_minimum_age
          status: varied
          provenance: <source>
          rationale: <why varied>
      data: []
      structures: []
      scenarios: []
      numerics: []
  budget:
    maximum_runs: 1000
    reserve_fraction: 0.1
  stopping:
    criteria:
      - metric: <declared metric>
        threshold: 0.05
        consecutive_checks: 2
  robustness:
    checks:
      - name: <declared check>
        rationale: <why required>
  stochastic:
    target: conditional_mean
    random_source: simulator_seed
    intrinsic_variance: report
    common_random_numbers: within_trajectory
    allocation:
      phase: pilot
      variance_quantile: 0.9
      target_mcse: 0.01
      minimum_replications: 2
      maximum_replications: 20
      selected_replications: null
      provenance: null
experiment:
  method: canonical_morris
  seeds: [1301, 1302]
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: <study-specific support source>
  matching_minimum_age:
    values: [0, 5, 12]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: <study-specific support source>
analysis:
  convergence_checkpoints: [2, 4, 8]
  screening_top_k: 1
```

### Sobol de démonstration

```yaml
experiment:
  method: sobol
  seeds: [1401, 1402]
  base_sample_size: 16
```

`base_sample_size` est une puissance de deux supérieure ou égale à 2.
`design_seed`, si elle est écrite, est un entier non signé 64 bits. Les graines
deviennent le facteur `seed` et doivent compter au moins deux valeurs. La
méthode exige une charte de démonstration.

Cette méthode est réservée à la démonstration et conserve son bootstrap des
lignes à brouillage unique. Son manifeste enregistre
`inferenceStatus: demonstration_noninferential`.

### Sobol multi-scramble

Les campagnes scientifiques de priorisation des facteurs (FP) et de fixation des
facteurs (FF) utilisent cette méthode :

```yaml
experiment:
  method: multiscramble_sobol
  seeds: [44011, 44012, 44013, 44014]
  seeds_provenance: Declared finite simulator-seed nuisance support
  base_sample_sizes: [2, 4, 8, 16]
  scramble_seeds: [101, 202, 303, 404]
```

`base_sample_sizes` est non vide, triée, commence par une puissance de deux d'au
moins 2, et chaque valeur double exactement la précédente. `scramble_seeds`
contient au moins quatre entiers non signés 64 bits uniques. `design_seed`
n'est pas permise. La taille maximale planifiée est le dernier point de
contrôle.

Les graines forment le support fini du facteur `seed`. Sa distribution
scientifique est implicitement `discrete_uniform` avec `transform: identity`,
et `seeds_provenance`, une chaîne non vide requise, indique d'où vient ce
support. Chaque paramètre déclaré a aussi besoin de sa propre distribution.

La charte scientifique doit avoir une dépendance indépendante et l'objectif
`fp` ou `ff` ; le criblage est refusé. FF exige une `practical_tolerance`
adimensionnelle de part de variance d'ordre total dans `[0,1]`. La graine est
explicitement non actionnable ; ses lignes agrégées et par brouillage gardent
des indices diagnostiques mais ont `is_actionable: false`, un rang nul et une
tolérance FF nulle. Le support fini n'estime pas un bruit intrinsèque non
restreint. L'implémentation refuse les plans au-delà de 1 000 000 positions de
design ou de 20 000 000 cellules de table Arrow, avant toute allocation QMC ou de
table ; réduisez le design déclaré plutôt que de contourner ces garde-fous.

## `parameters`

Chaque entrée nomme un paramètre eqasim et liste les valeurs à essayer. La forme
courte est une simple liste :

```yaml
parameters:
  sampling_rate: [0.001, 0.002, 0.003]
  income_assignation_method: [uniform, bhepop2]
```

Elle est strictement équivalente à la forme longue avec une seule clé `values`,
elle aussi acceptée :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
```

Les valeurs permises sont `int`, `float` fini ou chaîne non vide sans NUL. Elles
sont homogènes dans chaque domaine. Booléens, `null`, listes, mappings, NaN et
infinis sont refusés. Le nom doit être une option synpp sûre, une clé active de
`model.config.config` et un identifiant de facteur valide et non réservé. Le nom
`seed` est refusé : écrivez les graines dans [`experiment.seeds`](#graines).

« Clé active » signifie que le paramètre est écrit dans la section `config:` de
votre configuration eqasim. Le `config.yml` officiel n'en fixe que quelques-uns
(comme `sampling_rate`) ; pour faire varier un autre paramètre d'eqasim, par
exemple `matching_minimum_age`, écrivez-le d'abord dans `config:` avec sa
valeur habituelle, puis listez-le sous `parameters`.

Grid et OAT discret exigent au moins un facteur. À la création ou à la
validation du plan, chaque domaine Grid doit être non vide et homogène. L'OAT
discret exige des domaines numériques homogènes d'au moins deux niveaux
distincts et strictement croissants. Morris canonique et Sobol exigent au moins
deux valeurs scalaires homogènes et distinctes par facteur ; les supports
canoniques peuvent être numériques ou textuels, car la projection passe par la
distribution déclarée.

Pour inspecter les clés candidates et leurs types réels avant d'écrire
`parameters`, exécutez depuis le dossier eqasim (remplacez `config.yml` par
votre fichier de configuration) :

```bash
uv run --locked python - <<'PY'
from pathlib import Path
import yaml

document = yaml.safe_load(Path("config.yml").read_text())
for name, value in sorted(document["config"].items()):
    print(f"{name}: {type(value).__name__} = {value!r}")
PY
```

Ne gardez qu'une clé scientifique active et non réservée, avec des valeurs du
même type que l'option synpp correspondante.

### Sous une charte scientifique

Avec `charter.status: scientific`, la forme courte est refusée : chaque
paramètre exige la forme longue avec exactement `values` et `distribution` :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Bounds declared in protocol version 2
```

Pour les domaines actuels définis par une liste, seuls
`family: discrete_uniform` et `transform: identity` sont acceptés. `provenance`
est une chaîne non vide. Les déclarations de famille et de transformation sont
conservées comme données typées de la méta-configuration, afin que de futurs
échantillonneurs puissent étendre le vocabulaire. Un support uniforme discret
varié contient au moins deux valeurs distinctes ; l'uniformité porte sur les
points du support, jamais sur des positions répétées de la liste. Le facteur
graine de Sobol multi-scramble tient, lui, sa distribution de
`experiment.seeds_provenance`.

### Configurations nommées

Une clé du modèle contenant une liste ordonnée peut aussi utiliser des
configurations nommées. Le mécanisme est générique ; `matching_attributes` n'est
qu'un exemple représentatif :

```yaml
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
```

La clé du modèle doit exister et contenir une liste de chaînes. Chaque étiquette
est une chaîne unique, non vide et sans NUL. Chaque contenu est une liste
ordonnée non vide de chaînes uniques, non vides et sans NUL, et deux étiquettes
ne peuvent pas désigner le même contenu. L'ordre du contenu est conservé.

Grid est le premier design recommandé. Sobol de démonstration est accepté. Sobol
multi-scramble n'est accepté que si les étiquettes ont une distribution
scientifique défendable ; une déclaration scientifique ajoute le même voisin
`distribution` exact que pour les supports scalaires :

```yaml
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Protocol-defined matching configurations
```

La liste des étiquettes est le support du facteur utilisé par le design et le
contrat scientifique. La colonne du plan et `RunSpec.parameters` conservent les
étiquettes ; le contenu résolu est haché dans `runId`. L'OAT discret et Morris
canonique refusent les facteurs structurés, car l'ordre arbitraire d'étiquettes
nominales ne peut pas définir leurs pas. À l'exécution, une liste déclarée et
modifiée est écrite dans un instantané YAML sûr propre à la tentative et omise
des options de ligne de commande synpp ; les listes ou mappings modifiés non
déclarés restent invalides.

## `analysis`

Le bloc est facultatif. Chaque méthode accepte un ensemble fixe de clés ; toute
autre clé est refusée, y compris une clé qui appartient à une autre méthode.

| Méthode | Clés permises | Valeurs par défaut |
| --- | --- | --- |
| `grid`, `discrete_oat` | `completeness_threshold` | `completeness_threshold: 1.0` |
| `sobol` | `completeness_threshold`, `bootstrap_resamples`, `confidence_level` | `1.0`, `999`, `0.95` |
| `multiscramble_sobol` | `completeness_threshold`, `confidence_level` | `1.0`, `0.95` |
| `canonical_morris` | `completeness_threshold`, `bootstrap_resamples`, `confidence_level`, `convergence_checkpoints`, `screening_top_k` | `1.0`, `999`, `0.95` ; les deux dernières n'ont pas de valeur par défaut |

`completeness_threshold` est la part minimale des runs attendus qui doivent être
terminés pour qu'`analyze` publie ses résultats. C'est un nombre fini dans
`[0, 1]` ; `1` et `0` peuvent s'écrire comme des entiers. L'OAT discret, Morris
canonique, Sobol et Sobol multi-scramble exigent `1.0` : seul Grid peut
l'abaisser.

`bootstrap_resamples` est un entier dans `[1, 2^63-1]` ; Morris canonique en
exige au moins 100. `confidence_level` est un float fini strictement compris
entre 0 et 1. Sobol multi-scramble n'a pas de réglage de bootstrap : ses tailles
de convergence viennent uniquement de `experiment.base_sample_sizes`.

Morris canonique a toujours besoin d'un bloc `analysis`, car deux de ses clés
n'ont pas de valeur par défaut :

```yaml
analysis:
  convergence_checkpoints: [2, 4, 8]
  screening_top_k: 2
```

Les points de contrôle sont des entiers triés, uniques, d'au moins 2, et se
terminent à `trajectories`. `screening_top_k` est compris entre 1 et le nombre
de facteurs. Le bootstrap rééchantillonne des trajectoires entières, après que
les effets ont d'abord été moyennés sur les graines de réplication. Il fournit
une preuve d'incertitude de design fini conditionnelle au contrat scellé, pas une
certification automatique des exigences d'arrêt ou de robustesse de la charte.

## `outputs`

`outputs` liste les fichiers de chaque run qu'eqasim-sa conserve, et les
colonnes que les indicateurs peuvent y lire. Au moins une sortie est requise :

```yaml
outputs:
  persons: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
```

Chaque sortie accepte cinq clés, toutes facultatives pour une sortie connue :

| Clé | Contenu |
| --- | --- |
| `file` | chemin du fichier, relatif au dossier de sortie d'eqasim (`output_path`) |
| `separator` | séparateur de colonnes d'un fichier CSV, un seul caractère |
| `category` | la colonne des classes (par exemple les classes d'âge) |
| `simulated` | la colonne des valeurs produites par la simulation |
| `references` | les colonnes des valeurs de référence (recensement, enquête…) |

Un nom de sortie ne contient que des lettres, des chiffres, `_` et `-`. Les
noms qui ne diffèrent que par la casse, et les noms réservés par Windows, sont
refusés sur tous les systèmes.

### Sorties connues et leurs fichiers

eqasim-sa connaît les douze fichiers qu'eqasim écrit dans ses étapes standard.
Pour ces noms, `file` et `separator` ont une valeur par défaut : `persons: {}`
suffit.

| Sorties | `file` par défaut | `separator` par défaut | Écrites par l'étape eqasim |
| --- | --- | --- | --- |
| `persons`, `households`, `activities`, `trips`, `vehicle_types`, `vehicles` | `{prefix}<nom>.csv` | `;` | `synthesis.output` |
| `age`, `age_purpose`, `distance`, `license`, `nbr_vehicle`, `travel` | `analysis_population/{prefix}<nom>.csv` | `,` | `analysis.synthesis.population` |

`{prefix}` désigne l'`output_prefix` de votre configuration eqasim (par défaut
`ile_de_france_` dans eqasim) ; c'est la seule valeur à remplacer permise dans `file`. Pour
tout autre nom, `file` est requis et `separator` vaut `,` par défaut. C'est
ainsi que les fichiers de départ conservent le fichier spatial des activités :

```yaml
outputs:
  activities_spatial:
    file: "{prefix}activities.gpkg"
```

`file` est un chemin relatif écrit avec `/`, sans `..`, `\`, `:` ni chemin
absolu. Vous pouvez aussi changer le `file` d'une sortie connue, par exemple
pour lire sous un nom connu un fichier écrit par votre propre étape eqasim ; la
vérification d'étape décrite dans [Ce qu'eqasim exécute](#ce-queqasim-exécute)
ne s'applique alors pas à elle.

Le chargement refuse aussi, avec un message qui nomme la sortie :

- un `file` dont l'extension, une fois `{prefix}` remplacé, ne peut pas nommer
  un fichier d'archive (lettres, chiffres, `_` et `-` seulement ; par exemple
  `.csv~` est refusé) ;
- quand votre configuration eqasim fixe `config.output_formats`, une sortie
  connue de la population (`persons`, `households`, `activities`, `trips`,
  `vehicle_types`, `vehicles`) laissée à son fichier `.csv` par défaut alors
  qu'`output_formats` ne contient pas `csv`, ou une sortie `.gpkg` alors qu'il
  ne contient pas `gpkg` : eqasim n'écrirait pas ce fichier ;
- un indicateur (`share`, `value`, `tv_distance`) sur une sortie dont le `file`
  n'est pas un fichier `.csv` : les indicateurs ne lisent que des tables CSV.

### Ce qui est archivé

À la fin de chaque run réussi, chaque fichier déclaré est copié à l'octet près
dans l'archive du run, sous le nom `<nom de la sortie><extension du fichier>` :
`age.csv`, `persons.csv`, `activities_spatial.gpkg`, etc. Le manifeste du run
enregistre pour chaque fichier son chemin d'origine sous le dossier de sortie
d'eqasim (par exemple `analysis_population/nantes_age.csv`), sa taille et son
SHA-256. Un fichier déclaré qu'eqasim n'a pas écrit fait échouer le run avec un
message qui nomme le chemin attendu et rappelle de vérifier `run:` dans votre
configuration eqasim. Les fichiers non déclarés ne sont pas conservés.

Les [scripts d'analyse](#analyses) ne lisent que ces fichiers archivés :
déclarez, avant de planifier, tous les fichiers que vous voudrez peut-être
analyser plus tard (population, trajets, activités et leurs fichiers
spatiaux `.gpkg`).

Seuls `file` et `separator` déterminent ce qui est archivé : seules ces deux
clés, avec le nom de la sortie, sont scellées dans le plan. `category`,
`simulated` et `references` relèvent de l'analyse : vous pouvez les ajouter ou
les corriger après les runs et recalculer l'analyse sans rien relancer.
Ajouter, retirer ou renommer une sortie, ou changer son `file` ou son
`separator`, demande une nouvelle campagne ; reprendre l'ancienne s'arrête avec
un message tel que `output 'age' changed since plan: separator`.

### Colonnes lues par les indicateurs

`category`, `simulated` et `references` nomment des colonnes d'une sortie CSV.
Elles ne sont nécessaires que pour les sorties utilisées par `tv_distance` ou
`share` ; `category` et `simulated` sont des chaînes distinctes et non vides,
et `references` est une liste, éventuellement vide et sans doublon, de chaînes
non vides distinctes des deux autres colonnes. Les noms doivent reproduire
exactement l'en-tête du CSV, espaces et casse compris.

Les fichiers CSV sont d'abord lus comme du texte. Si la première colonne a un
en-tête vide (l'index de lignes qu'écrit pandas), elle est retirée. Les
colonnes qu'un indicateur utilise comme nombres (`simulated`, les colonnes de
référence, la colonne lue par `value`) sont ensuite converties explicitement ;
une cellule qui n'est pas un nombre fini arrête l'analyse avec une erreur qui
nomme la colonne. Les libellés de classe restent du texte : une classe écrite
`0` dans le fichier est la chaîne `"0"`.

Pour voir les noms exacts des colonnes, ouvrez un fichier archivé dans un
tableur ou affichez sa première ligne :

```bash
head -n 1 campaigns/mon-analyse-sa-calibration/archives/<runId>/age.csv
```

Remplacez `<runId>` par un identifiant réel.

## `analyses`

`analyses` déclare des scripts Python qui calculent de nouvelles tables à
partir des fichiers archivés de chaque run, au moment d'`analyze`. Chaque table
est une *sortie d'analyse*, qui porte le nom de son analyse et que
`tv_distance`, `share` et `value` lisent comme une sortie. eqasim n'exécute
rien : les scripts ne lisent que les archives des runs, si bien qu'on peut
ajouter ou modifier des analyses après les runs. Le guide
[Écrire un script d'analyse](analysis-scripts.md) explique comment en écrire
un.

```yaml
outputs:
  persons: {}
  trips: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
analyses:
  commute:
    script: analyses/commute.py
    inputs: [trips, activities_spatial, persons]
indicators:
  - value: {output: commute, column: mean_commute_km, where: {group: all}}
```

Chaque analyse accepte cinq clés :

| Clé | Présence | Contenu |
| --- | --- | --- |
| `script` | requise | chemin du fichier `.py`, relatif à ce fichier YAML ou absolu |
| `inputs` | requise | liste non vide et sans doublon des sorties transmises au script |
| `category` | facultative | colonne de la table renvoyée qui contient les classes |
| `simulated` | facultative | colonne de la table renvoyée qui contient les valeurs simulées |
| `references` | facultative, vide par défaut | colonnes de la table renvoyée qui contiennent les valeurs de référence |

Le nom d'une analyse suit les règles des noms de sortie (lettres, chiffres,
`_` et `-`, aucun nom réservé de Windows). Il doit différer de tout nom de
sortie et de tout autre nom d'analyse, sans tenir compte de la casse, et `logs`
est réservé (le cache y range ses journaux d'erreur). `script` doit être un
fichier `.py` ordinaire existant ; son dernier composant ne doit pas être un
lien symbolique. Chaque nom de `inputs` doit être déclaré dans `outputs` avec
un fichier `.csv` ou `.gpkg` ; une analyse ne peut pas lire une autre analyse.
`category`, `simulated` et `references` suivent les règles des
[colonnes lues par les indicateurs](#colonnes-lues-par-les-indicateurs) et
nomment des colonnes de la table que renvoie le script.

Les analyses ne sont pas scellées dans le plan : elles sont lues dans la
méta-configuration courante à chaque `analyze`, comme `category`, `simulated`,
`references` et les indicateurs. Seules les analyses lues par au moins un
indicateur sont calculées. Sans bloc `indicators`, les indicateurs par défaut
ignorent les sorties d'analyse, et le `primary_indicator` d'une charte
scientifique ne peut pas en lire une (`primary_indicator must not use analysis
output 'commute'`). Le script s'exécute avec l'interpréteur d'eqasim, choisi
par [`execution.python`](#execution).

## `indicators`

Un indicateur transforme les sorties d'un run en un nombre unique, que
l'analyse de sensibilité étudie ensuite. Trois types sont intégrés :

| Type | Nom du résultat | Ce qu'il calcule |
| --- | --- | --- |
| `tv_distance` | `tv_distance(age, ref=INSEE)` | distance de variation totale entre la distribution simulée et une distribution de référence |
| `share` | `share(age, 0-10)` | proportion d'une classe dans une distribution simulée |
| `value` | `value(summary, mean_income, zone=44)` | un nombre lu dans une cellule d'une table |

### Valeur par défaut

Sans clé `indicators`, eqasim-sa calcule `tv_distance` pour chaque sortie et
chacune de ses références. Les sorties déclarées sans référence sont ignorées.
Avec les sorties des fichiers de départ, on obtient trois indicateurs :
`tv_distance(age, ref=INSEE)`, `tv_distance(distance, ref=HTS)` et
`tv_distance(nbr_vehicle, ref=HTS)`.

`indicators: []` désactive les indicateurs. Seul Grid accepte l'absence
d'indicateur ; l'OAT discret, Morris canonique, Sobol et Sobol multi-scramble en
exigent au moins un.

### Choisir les indicateurs

Chaque entrée de la liste a exactement une clé, le type d'indicateur :

```yaml
indicators:
  - tv_distance: {output: age, reference: INSEE}     # une sortie contre une référence
  - tv_distance: {output: distance}                  # une sortie contre chacune de ses références
  - tv_distance: {outputs: [distance, nbr_vehicle]}  # plusieurs sorties, chacune contre chacune de ses références
  - share: {output: nbr_vehicle, category: "0"}      # proportion d'une classe
  - value: {output: summary, column: mean_income, where: {zone: "44"}}  # une cellule
```

Ces lignes illustrent chaque forme. Écrites ensemble, elles calculeraient deux
fois `tv_distance(distance, ref=HTS)`, ce qui est refusé : une vraie liste
nomme chaque indicateur une seule fois.

La forme en bloc équivaut à la forme en ligne :

```yaml
indicators:
  - tv_distance:
      output: age
      reference: INSEE
```

Chaque indicateur reçoit un nom, montré dans le premier tableau ; le
`primary_indicator` d'une charte scientifique doit être égal à l'un de ces
noms. Les noms des indicateurs résolus doivent être uniques dans l'analyse.

### Distance de variation totale (`tv_distance`)

Une entrée `tv_distance` a exactement `output` et `reference`, ou seulement
`output`, ou seulement `outputs`. Les noms sont des chaînes non vides ; `outputs`
est une liste non vide sans doublon. Chaque sortie doit être déclarée dans
`outputs` avec `category` et `simulated`, chaque référence doit être l'une des
références de cette sortie, et une sortie nommée sans référence doit en avoir
au moins une.

L'indicateur lit les colonnes `category`, `simulated` et de référence. Les deux
colonnes de masse doivent être des nombres finis, non négatifs et de somme
positive. Les libellés de classe doivent être non vides et uniques. Les deux
colonnes sont normalisées séparément, puis :

`TV = 0.5 × Σ |p_i - q_i|`, avec un résultat borné dans `[0, 1]`.

0 signifie des distributions identiques ; 1, des distributions sans aucune
classe commune.

### Part d'une classe (`share`)

```yaml
indicators:
  - share: {output: age, category: "0-10"}
```

Une entrée `share` a exactement `output` et `category`. La sortie doit déclarer
`category` et `simulated`. La colonne simulée est normalisée pour sommer à 1,
et l'indicateur vaut la proportion de la ligne dont la classe est égale à
`category`. Son nom est `share(age, 0-10)`.

`category` doit être une chaîne YAML entre guillemets, même quand la classe
ressemble à un nombre ou à un booléen : écrivez `category: "0"` ou
`category: "True"`, jamais `category: 0`. Une classe absente du fichier arrête
l'analyse avec une erreur.

### Un nombre lu dans une table (`value`)

```yaml
indicators:
  - value: {output: summary, column: mean_income}
  - value: {output: summary, column: mean_income, where: {zone: "44"}}
```

Une entrée `value` a `output` et `column`, et facultativement `where`. Elle lit
la cellule de `column` dans l'unique ligne choisie par `where`. Sans `where`, le
fichier doit avoir exactement une ligne. Avec `where`, chaque colonne listée
doit être égale au texte donné ; exactement une ligne doit correspondre. La
cellule doit être un nombre fini. Les noms sont `value(summary, mean_income)` et
`value(summary, mean_income, zone=44)`.

Comme pour `share.category`, chaque valeur de `where` doit être une chaîne YAML
entre guillemets (`zone: "44"`, pas `zone: 44`). La sortie n'a besoin ni de
`category`, ni de `simulated`, ni de `references`.

### Vos propres calculs

Quand il vous faut une grandeur que les trois types ci-dessus ne calculent pas
(une distance moyenne domicile-travail, un taux par zone, une part modale),
écrivez un [script d'analyse](analysis-scripts.md) : il lit les fichiers
archivés de chaque run et renvoie une petite table, que `value`, `share` ou
`tv_distance` lisent ensuite. Il peut être ajouté après les runs, sans relancer
eqasim. Voir [`analyses`](#analyses).

Vous pouvez aussi calculer la grandeur dans eqasim : écrivez une étape
d'analyse eqasim qui enregistre une petite table CSV dans le dossier de sortie,
ajoutez cette étape à `run:` dans votre configuration eqasim, déclarez le
fichier sous `outputs` avant de planifier et lisez-le avec `value`. Par
exemple, si votre étape écrit `analysis_population/{prefix}summary.csv` :

```yaml
outputs:
  summary:
    file: "analysis_population/{prefix}summary.csv"
indicators:
  - value: {output: summary, column: mean_income}
```

Comme l'étape fait partie du code d'eqasim, elle entre dans l'empreinte du code
et dans l'identité de la campagne, et son fichier CSV est archivé avec chaque
run ; la modifier demande une nouvelle campagne.

## `execution`

```yaml
execution:
  scratch: ../scratch
  workers: 2
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

Le bloc décrit la façon d'exécuter les runs sur cette machine. Il est facultatif
pour `plan`, `analyze` et `show-config`, mais requis pour `run`, `check` et
`estimate`.

| Clé | Présence | Contenu |
| --- | --- | --- |
| `scratch` | requise | répertoire de travail temporaire des runs |
| `backend` | facultative, défaut `local` | `local` ou `slurm` |
| `workers` | facultative, défaut `1` | nombre de runs exécutés en parallèle |
| `invocation` | facultative, défaut `[uv, run, --locked, python, -m, synpp]` | commande qui lance synpp |
| `python` | facultative, défaut `invocation` sans `-m synpp` | commande de l'interpréteur Python qui exécute les [scripts d'analyse](#analyses) |
| `machine_options` | facultative, vide par défaut | options d'environnement eqasim propres à cette machine |
| `slurm` | facultative | ressources du cluster, seulement avec `backend: slurm` |

`workers` est un entier dans `[1, 1024]`. `scratch` est une chaîne non vide, sans
NUL ni traversée de lien symbolique, qui doit déjà désigner un répertoire.
`invocation` est une liste non vide de chaînes non vides sans NUL.

`python` est aussi une liste non vide de chaînes non vides sans NUL. Il est
exécuté dans le dossier eqasim et doit fournir pandas (l'environnement
d'eqasim le fournit). Sans lui, eqasim-sa retire le `-m synpp` final
d'`invocation` ; sans bloc `execution`, il utilise
`[uv, run, --locked, python]`. Quand `invocation` ne se termine pas par
`-m synpp`, un fichier qui déclare `analyses` doit écrire `python`, sinon le
chargement échoue avec `analyses need execution.python because
execution.invocation does not end with -m synpp`.

### `machine_options`

Ce sont les options eqasim qui dépendent de la machine, pas de l'étude. Seules
les options présentes dans le YAML modèle scellé sont permises : `data_path`,
`git_binary`, `java_binary`, `java_memory`, `maven_binary` et `processes`. La
valeur doit conserver exactement le type du YAML modèle et être transportable
comme scalaire synpp ; les booléens ne le sont pas. `data_path` doit se résoudre
en un répertoire existant sans traverser de lien symbolique. Pour SLURM,
`processes` doit être un entier strictement positif.

Les options machine ne contribuent pas au `runId` : elles décrivent
l'infrastructure, pas un facteur scientifique. Elles ne sont appliquées qu'au
moment de l'exécution. Un paramètre scientifique comme `sampling_rate` est
refusé ici ; faites-le varier sous `parameters`.

### `slurm`

```yaml
execution:
  backend: slurm
  workers: 20
  scratch: /scratch/mon-etude
  machine_options:
    processes: 4
  slurm:
    timeout_min: 1440
    mem_gb: 16.0
    partition: compute
    account: equipe-transport
    qos: normal
```

`slurm` est interdit avec le backend local. Toutes ses clés sont facultatives.
`timeout_min` vaut 1440 par défaut et doit être un entier positif. `mem_gb` est
un entier ou un float fini supérieur ou égal à `1/1024` ; `null` signifie
absent. `partition`, `account` et `qos` sont `null` ou des identifiants sûrs
suivant le même motif que `campaign`. `cpus_per_task` provient de
`machine_options.processes`, ou vaut 1.

## `calibration`

```yaml
calibration:
  meta_configs: [00-calibration.yml]
```

Le mapping contient exactement `meta_configs`, une liste non vide de chaînes. Les
chemins désignent des fichiers ordinaires, sont dédupliqués, ne peuvent pas
désigner le fichier lui-même et ne peuvent pas contenir à leur tour de bloc
`calibration`. Des chemins supplémentaires peuvent être passés à `check` et
`estimate` par la CLI.

## `charter`

La charte optionnelle classe les revendications permises par une campagne. Son
absence équivaut à une charte de démonstration. Une
démonstration explicite a exactement cette forme, sans clé supplémentaire :

```yaml
charter:
  status: demonstration
```

Une campagne de démonstration valide la configuration, l'exécution, le stockage
et la plomberie d'analyse. Elle ne peut soutenir aucune revendication
inférentielle ou décisionnelle. Grid, l'OAT discret et `sobol` sont des designs de
démonstration. L'OAT discret est la démonstration à niveaux adjacents et n'est
pas Morris canonique. Les méthodes prises en charge sont `grid`, `discrete_oat`,
`canonical_morris`, `sobol` et `multiscramble_sobol`.

Une charte scientifique emploie le schéma strict complet suivant :

```yaml
charter:
  status: scientific
  decision:
    claim: Identifier les entrées qui doivent rester à l'étude
    use: diagnostic
    setting: Évaluation des politiques de synthèse de population nantaise
    objective: screening
    estimand: Distance de variation totale espérée sur les seeds de simulation
    primary_indicator: tv_distance(age, ref=INSEE)
  uncertainty:
    dependence:
      kind: independent
      rationale: Le design Morris canonique suppose les entrées indépendantes
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
  stochastic:
    target: conditional_mean
    random_source: simulator_seed
    intrinsic_variance: report
    common_random_numbers: within_trajectory
    allocation:
      phase: pilot
      variance_quantile: 0.9
      target_mcse: 0.01
      minimum_replications: 2
      maximum_replications: 20
      selected_replications: null
      provenance: null
```

`decision.claim`, `setting`, `estimand` et `primary_indicator` sont des chaînes
non vides. `setting` décrit le contexte décisionnel et `estimand` la quantité à
apprendre. `use` vaut `diagnostic` ou `prognostic`; `objective` vaut criblage
(`screening`), priorisation (`fp`) ou fixation (`ff`). `primary_indicator` doit
être exactement le nom d'un indicateur résolu. Le float fini non négatif
`practical_tolerance` est requis exactement pour `ff` et interdit sinon. Une
configuration scientifique exige au moins un indicateur. Cette règle générique
est indépendante de la méthode. Pour `multiscramble_sobol`, cette tolérance est
spécifiquement une part de variance d'ordre total sans dimension dans `[0,1]`.

La dépendance est soit `independent`, avec exactement `kind` et un `rationale`
non vide, soit `declared`, avec en plus une liste `groups` non vide. Chaque groupe
a exactement `factors`, `model`, `provenance`, au moins deux noms distincts de
paramètres déclarés, et aucun facteur ne chevauche deux groupes. L'inventaire a
exactement les cinq catégories montrées. Chaque élément a exactement `name`,
`status`, `provenance`, `rationale`; les textes sont non vides, le statut vaut
`varied`, `fixed` ou `excluded`, et les noms sont uniques par catégorie. Les noms
`varied` de l'inventaire des paramètres doivent correspondre exactement aux noms
déclarés sous `parameters`, plus `seed` pour Sobol multi-scramble, dont le
facteur graine provient de `experiment.seeds`.

`maximum_runs` et chaque `consecutive_checks` sont des entiers signés 64 bits
positifs. `reserve_fraction` est un float fini dans `[0, 1)`. `criteria` est non
vide ; chaque critère contient exactement `metric`, un float fini positif
`threshold`, et `consecutive_checks`, avec des métriques uniques.
`robustness.checks` est non vide. Chaque contrôle contient exactement un `name`
non vide et unique ainsi qu'un `rationale` non vide.

Toute charte scientifique possède exactement un mode `stochastic` typé. La forme
`conditional_mean` ci-dessus est réservée à `canonical_morris` et ses constantes
sont exactes. Le quantile est un float fini dans `(0,1]`, le MCSE un float fini
positif dans les unités de l'indicateur primaire, le minimum vaut au moins 2 et
le maximum lui est supérieur ou égal. En phase `pilot`, sélection et provenance
sont nulles et l'expérience fournit au moins le minimum de seeds de base. En
phase `production`, la sélection est dans les bornes, la provenance est non vide
et le nombre de seeds lui est exactement égal.

`multiscramble_sobol` exige au contraire exactement :

```yaml
stochastic:
  target: finite_seed_factor
  random_source: simulator_seed
  intrinsic_variance: not_identified
  common_random_numbers: none
  support_interpretation: declared_finite_nuisance
```

Il n'a aucune allocation. Le facteur graine, nommé `seed` dans la charte et
construit à partir de `experiment.seeds`, reste un élément varié requis de
l'inventaire des paramètres, mais son support fini est une nuisance : il
n'identifie ni importance contrôlable ni bruit intrinsèque du simulateur.

`load_meta_config` valide la structure de la charte. Les méthodes scientifiques
courantes sont `canonical_morris` pour `screening` indépendant avec
`conditional_mean`, et `multiscramble_sobol` pour `fp` ou `ff` indépendant avec
`finite_seed_factor`. L'OAT discret et Sobol de démonstration (`sobol`) sont
refusés par une charte scientifique.

Les métadonnées du plan canonique conservent un `scientificContract` normalisé.
Sa racine contient exactement `charter` et `parameters`. La charte normalisée
contient exactement `status`, `decision`, `uncertainty`, `budget`, `stopping`,
`robustness` et `stochastic`; `stopping` et `robustness` sont directement les listes d'éléments.
Le criblage sérialise `practical_tolerance: null` et l'indépendance
`groups: []`. Les paramètres, triés par nom, contiennent exactement nom, support
et distribution correspondante. Le cœur vérifie toute cette forme et son égalité
avec le design avant publication.

## Exemples complets valides

Les exemples ci-dessous sont complets. Avant de les charger, eqasim-sa vérifie
aussi la machine : les chemins doivent exister, `model.root` doit être un
répertoire qui contient le code d'eqasim, `model.config` doit être un fichier
ordinaire dont la liste `run:` produit les sorties connues déclarées, le
manifeste et le lockfile doivent être des fichiers ordinaires,
le répertoire scratch doit exister, et les facteurs et options machine doivent
exister dans le YAML modèle avec les types indiqués. Le fichier
`00-calibration.yml` référencé doit aussi exister et ne pas contenir de bloc
`calibration`. Chaque `script` d'analyse doit exister.

Les fichiers rendus par `init` sont des fichiers de départ de démonstration :
[calibration](../src/eqasim_sa/templates/00-calibration.yml),
[Grid](../src/eqasim_sa/templates/01-grid.yml),
[OAT discret](../src/eqasim_sa/templates/02-discrete-oat.yml) et
[Sobol](../src/eqasim_sa/templates/03-sobol.yml). Aucun fichier de départ Morris canonique
ni Sobol multi-scramble n'est généré ; le
[protocole scientifique de Nantes](../protocols/nantes-scientific/README.md)
contient des fichiers scientifiques complets.

### Grid

Le fichier de départ Grid figure dans [Un premier fichier](#un-premier-fichier).

### Le même Grid avec toutes les valeurs par défaut écrites

Voici ce qu'affiche `show-config configs/01-grid.yml` pour ce fichier de départ. C'est
aussi une méta-configuration valide : les deux fichiers décrivent la même
campagne.

```yaml
campaign: mon-analyse-sa-grid
campaign_root: ../campaigns/mon-analyse-sa-grid
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
  lockfile: ../../eqasim-france/uv.lock
experiment:
  method: grid
  seeds:
  - 1201
  - 1202
parameters:
  sampling_rate:
    values:
    - 0.001
    - 0.002
analysis:
  completeness_threshold: 1.0
outputs:
  persons:
    file: '{prefix}persons.csv'
    separator: ;
  households:
    file: '{prefix}households.csv'
    separator: ;
  trips:
    file: '{prefix}trips.csv'
    separator: ;
  activities:
    file: '{prefix}activities.csv'
    separator: ;
  activities_spatial:
    file: '{prefix}activities.gpkg'
    separator: ','
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references:
    - INSEE
    file: analysis_population/{prefix}age.csv
    separator: ','
  distance:
    category: Distance class
    simulated: Proportion_EQASIM
    references:
    - HTS
    file: analysis_population/{prefix}distance.csv
    separator: ','
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references:
    - HTS
    file: analysis_population/{prefix}nbr_vehicle.csv
    separator: ','
indicators:
- tv_distance:
    outputs:
    - age
    - distance
    - nbr_vehicle
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
  backend: local
  invocation:
  - uv
  - run
  - --locked
  - python
  - -m
  - synpp
  python:
  - uv
  - run
  - --locked
  - python
calibration:
  meta_configs:
  - 00-calibration.yml
```

Sous cette forme affichée, les sorties connues montrent leur `file` et leur
`separator` par défaut.

### OAT discret

```yaml
# Discrete adjacent-level OAT demonstration: eight positions and six unique runs, not Morris inference.
campaign: mon-analyse-sa-discrete_oat
experiment:
  method: discrete_oat
  seeds: [1301, 1302]
  trajectories: 2
  design_seed: 20260817
parameters:
  sampling_rate: [0.001, 0.002, 0.003]
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
  distance:
    category: Distance class
    simulated: Proportion_EQASIM
    references: [HTS]
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references: [HTS]
calibration:
  meta_configs: [00-calibration.yml]
# --- Machine-specific values ---
# eqasim-sa init fills in campaign (above), the paths below and, when given,
# the eqasim command, and compares them when it is rerun: edit them only when
# the machine changes. workers and machine_options.processes are yours:
# change them freely.
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

### Sobol de démonstration

```yaml
# Single-scramble Sobol demonstration: 48 positions and 4 unique runs, not inferential evidence.
campaign: mon-analyse-sa-sobol
experiment:
  method: sobol
  seeds: [1401, 1402]
  base_sample_size: 8
parameters:
  sampling_rate: [0.001, 0.002]
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
  distance:
    category: Distance class
    simulated: Proportion_EQASIM
    references: [HTS]
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references: [HTS]
analysis:
  bootstrap_resamples: 99
calibration:
  meta_configs: [00-calibration.yml]
# --- Machine-specific values ---
# eqasim-sa init fills in campaign (above), the paths below and, when given,
# the eqasim command, and compares them when it is rerun: edit them only when
# the machine changes. workers and machine_options.processes are yours:
# change them freely.
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

### Grid avec des indicateurs choisis

Cette variante utilise une copie de la configuration eqasim de Nantes nommée
`config_nantes_sa.yml` (faite avec `cp config_nantes.yml config_nantes_sa.yml`),
placée à côté de `config_nantes.yml`, dans laquelle
`matching_minimum_age` est écrit sous `config:` et
`analysis.synthesis.population` figure dans `run:`. Elle ne garde que deux
distributions, choisit ses indicateurs, écrit son propre dossier de campagne et
utilise la forme longue pour un paramètre :

```yaml
campaign: mon-analyse-sa-grid-age
campaign_root: ../campaigns/grid-age
experiment:
  method: grid
  seeds: [1201, 1202]
parameters:
  sampling_rate:
    values: [0.001, 0.002]
  matching_minimum_age: [0, 5, 12]
outputs:
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references: [HTS]
indicators:
  - tv_distance: {output: age, reference: INSEE}
  - tv_distance: {output: nbr_vehicle}
  - share: {output: nbr_vehicle, category: "0"}
model:
  root: ../../eqasim-france
  config: config_nantes_sa.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
```

### Grid avec un script d'analyse

Cette variante du fichier de départ Grid ajoute la distance moyenne domicile-travail du
guide
[Écrire un script d'analyse](analysis-scripts.md#un-exemple-complet). Le script
est `configs/analyses/commute.py`, à côté de ce fichier :

```yaml
campaign: mon-analyse-sa-grid-commute
experiment:
  method: grid
  seeds: [1201, 1202]
parameters:
  sampling_rate: [0.001, 0.002]
outputs:
  persons: {}
  trips: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
analyses:
  commute:
    script: analyses/commute.py
    inputs: [trips, activities_spatial, persons]
indicators:
  - tv_distance: {output: age, reference: INSEE}
  - value: {output: commute, column: mean_commute_km, where: {group: all}}
calibration:
  meta_configs: [00-calibration.yml]
# --- Machine-specific values ---
# eqasim-sa init fills in campaign (above), the paths below and, when given,
# the eqasim command, and compares them when it is rerun: edit them only when
# the machine changes. workers and machine_options.processes are yours:
# change them freely.
model:
  root: ../../eqasim-france
  config: config.yml
  data_manifest: ../manifest.json
execution:
  workers: 2
  scratch: ../scratch
  machine_options:
    data_path: ../../eqasim-data
    processes: 1
```

L'`invocation` par défaut se termine par `-m synpp` : les scripts s'exécutent
donc avec `uv run --locked python` dans `../../eqasim-france`.

## Exemples invalides

Chaque fragment ci-dessous fait échouer un fichier par ailleurs valide, avec le
message indiqué.

Une graine déclarée comme paramètre :

```yaml
parameters:
  seed: [1, 2]
```

> declare simulation seeds in experiment.seeds, not parameters.seed

Une clé d'expérience mal orthographiée (`seed` au lieu de `seeds`) :

```yaml
experiment:
  method: grid
  seed: [1201, 1202]
```

> experiment (grid) keys must be among method, seeds; unknown: seed

Une clé d'analyse qui appartient à une autre méthode :

```yaml
analysis:
  bootstrap_resamples: 999
```

> analysis keys must be among completeness_threshold; unknown: bootstrap_resamples

Un indicateur sur une sortie non déclarée :

```yaml
indicators:
  - tv_distance: {output: income}
```

> invalid indicator combinations: tv_distance(income)

Une sortie qu'eqasim-sa ne connaît pas, déclarée sans son fichier :

```yaml
outputs:
  summary: {}
```

> output 'summary' is not a known eqasim output; declare its file (relative to
> eqasim's output_path, e.g. analysis_population/{prefix}summary.csv)

Une classe de `share` écrite comme un nombre YAML au lieu d'une chaîne entre
guillemets :

```yaml
indicators:
  - share: {output: nbr_vehicle, category: 0}
```

> share category must be a quoted string (e.g. "0", "True")

Une valeur de `where` écrite comme un nombre YAML :

```yaml
indicators:
  - value: {output: summary, column: mean_income, where: {zone: 44}}
```

> where values must be quoted strings

Un paramètre scientifique donné comme option machine :

```yaml
execution:
  machine_options:
    sampling_rate: 0.002
```

> machine_options 'sampling_rate' is not an allowed environment option

Un paramètre en forme courte sous une charte scientifique (la charte elle-même
est omise ici) :

```yaml
parameters:
  sampling_rate: [0.001, 0.002]
```

> factor 'sampling_rate' requires a distribution under a scientific charter (use
> the long form with values and distribution)

Un domaine non ordonné dans le fichier d'OAT discret, car l'OAT discret se
déplace entre niveaux adjacents croissants :

```text
parameters:
  sampling_rate: [0.001, 0.003, 0.002]
```

> discrete OAT factor 'sampling_rate' levels must be distinct and strictly
> increasing

Une entrée d'analyse qui n'est pas déclarée dans `outputs` :

```yaml
analyses:
  commute:
    script: analyses/commute.py
    inputs: [trips_spatial]
```

> analysis 'commute' input 'trips_spatial' is not declared in outputs

Un script d'analyse qui n'existe pas :

```yaml
analyses:
  commute:
    script: analyses/comute.py
    inputs: [trips]
```

> analysis 'commute' script 'analyses/comute.py' does not exist

Des analyses avec une invocation qui ne lance pas synpp comme module :

```yaml
execution:
  invocation: [eqasim-run]
```

> analyses need execution.python because execution.invocation does not end
> with -m synpp

Une clé dupliquée (ici avec en plus un alias YAML, refusé lui aussi) est
détectée avant toute autre vérification :

```text
parameters: &params {}
parameters: *params
```

> duplicate YAML key: 'parameters'
