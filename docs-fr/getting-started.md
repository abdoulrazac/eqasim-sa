# Démarrage

[English](../docs/getting-started.md) | **Français**

## Prérequis

- Linux/POSIX : le code utilise notamment `fcntl`, `flock`, `os.uname` et des groupes de processus.
- Python 3.13 exactement dans la plage déclarée `>=3.13,<3.14`.
- `uv` et Git disponibles.
- Un dépôt eqasim propre, avec un commit `HEAD`, un YAML suivi par Git et un `uv.lock`.
- Un répertoire de données existant sans liens symboliques pour `init`.
- L'environnement synpp requis par eqasim. `synpp` n'est pas une dépendance Python d'`eqasim-sa`.

## Installer avec uv

À la racine du dépôt `openmole-eqasim` :

```bash
ROOT="$PWD"
uv sync --project "$ROOT/eqasim-sa" --extra test --locked
uv sync --project "$ROOT/eqasim-france" --locked
SA=(uv run --project "$ROOT/eqasim-sa" --locked eqasim-sa)
```

Le lockfile d'`eqasim-sa` fixe l'environnement du runner. Le lockfile référencé
dans une méta-configuration entre, lui, dans le contexte scientifique scellé.

## Initialiser un projet

```bash
"${SA[@]}" init \
  --project-name mon-analyse-sa \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
```

`init` est strictement un générateur atomique de modèle. Il rend quatre YAML,
un README et un `manifest.json` contenant la taille et le SHA-256 de chaque
fichier de données. Il ne lance aucun sous-processus, ne vérifie pas
l'environnement synpp, ne planifie rien et n'exécute aucune étape du pipeline.

```text
mon-analyse-sa/
├── README.md
├── manifest.json
├── configs/
│   ├── 00-calibration.yml
│   ├── 01-grid.yml
│   ├── 02-discrete-oat.yml
│   └── 03-sobol.yml
├── campaigns/
└── scratch/
```

Une relance identique renvoie `status: unchanged` sans rehacher les données.
`--regenerate-data-manifest` remplace atomiquement le manifeste local, mais ne
modifie ni les YAML, ni les plans, ni les résultats. Sur un projet existant,
`init` vérifie d'abord que les fichiers statiques correspondent encore exactement
aux modèles attendus et que l'état durable a une forme reconnue. La régénération
renvoie `status: initialized`. Si un plan existe déjà, le nouveau SHA-256 du
manifeste change le contexte attendu : conservez l'ancien état et utilisez de
nouvelles racines de campagne ou un nouveau projet.

## Runner du projet généré

Le README généré définit un tableau Bash :

```bash
SA=(...)
```

Depuis un checkout source, le rendu utilise
`uv run --project /chemin/vers/eqasim-sa --locked eqasim-sa`. Dans un paquet
installé sans `pyproject.toml` source détectable, il utilise `eqasim-sa`. Toujours
invoquer le tableau avec `"${SA[@]}"` pour préserver les arguments.

## Première calibration des ressources

Depuis `mon-analyse-sa/`, sceller puis exécuter la calibration :

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml
```

Le premier `run` fournit les observations dont `check` et `estimate` ont besoin :
durée, octets durables et taille maximale du cache. Un contrôle statique peut
être lancé ensuite, mais il n'est pas un prérequis au bootstrap :

```bash
"${SA[@]}" check --meta-config configs/00-calibration.yml
```

## Première cible

Une fois les observations de calibration présentes, suivre cet ordre :

```bash
TARGET=configs/01-grid.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

`check` sans `--smoke` valide la méta-configuration, le plan, l'environnement et
l'espace estimé. `check --smoke` ajoute deux vrais runs temporaires et doit donc
être réservé à une validation explicite du pipeline. Il ne reprend pas
`execution.overrides` : il part du YAML modèle scellé, force
`sampling_rate: 0.001` et les deux premières seeds. En particulier, son
`data_path` est celui du modèle, pas `execution.overrides.data_path`.

Les modèles fournis sont de petites campagnes de démonstration. Leurs tailles ne
constituent pas une recommandation scientifique et `init` ne génère délibérément
aucun starter Morris canonique. Consultez [Paramètres et méthodes](parameters-and-methods.md)
avant d'étendre les domaines ou de rédiger une campagne scientifique propre à l'étude.

## Première cible structurée

Pour comparer des listes ordonnées `matching_attributes` nommées, committez
d'abord une clé active sous forme de liste dans le dépôt modèle propre, puis
dérivez une configuration Grid avec une nouvelle campagne/racine. Le
[tutoriel des configurations structurées](structured-configurations-tutorial.md)
couvre les six runs, les métadonnées du plan, l'inspection de l'analyse et les
limites scientifiques. `init` ne modifie pas le YAML modèle et ne génère pas
automatiquement cette cible.

## Checkpoints minimaux des modèles générés

Les commandes `plan` renvoient un objet JSON avec `status`, `designPositions` et
`uniqueRuns`. Avec les modèles non modifiés issus d'`init`, vérifiez :

| Étape | JSON minimal à la première exécution | Fichiers attendus après `run`/`analyze` |
| --- | --- | --- |
| Calibration | `plan`: `created`, 3 positions, 3 runs uniques; `run`: `completed`, 3 complétés | `campaigns/calibration/plan.parquet`, 3 marqueurs sous `runs/`, 3 archives |
| Grid | `plan`: `created`, 8 positions, 8 runs uniques; `run`: 8 complétés; `analyze`: `completed` | `indicators.parquet`, `replication-dispersion.parquet`, `output-summary.parquet`, `governed-report.json`, `analysis-generation.json` |
| OAT discret | `plan`: `created`, 8 positions, 6 runs uniques; `run`: 6 complétés; `analyze`: sensibilité `discrete_oat` | `indicators.parquet`, `replication-dispersion.parquet`, `discrete-oat-effects.parquet`, `output-summary.parquet`, `governed-report.json`, `analysis-generation.json` |
| Sobol | `plan`: `created`, 32 positions, 8 runs uniques; `run`: 8 complétés; `analyze`: sensibilité `sobol` | `indicators.parquet`, `sobol-indices.parquet`, `output-summary.parquet`, `governed-report.json`, `analysis-generation.json` |

Une relance identique de `plan` renvoie `unchanged`. Une campagne déjà complète
renvoie pour `run` `status: completed` et `total: 0`.

## Checklist de fin

- Les quatre plans correspondent aux cardinalités attendues ou à votre design documenté.
- Chaque `runId` attendu possède un marqueur valide et une archive lisible.
- `analyze` annonce `status: completed` et le taux de complétude attendu.
- Les schémas et colonnes des Parquet d'artefacts correspondent aux déclarations YAML.
- Les sorties de démonstration Grid, OAT discret et Sobol ont été inspectées, pas seulement créées.
- Le Sobol généré reste historique et non inférentiel. Une campagne scientifique
  multi-scramble doit être rédigée séparément; aucun starter n'est fourni.
- Le modèle, les données, le lockfile et la version d'`eqasim-sa` sont archivés ou traçables.

Voir aussi le [glossaire](glossary.md).
