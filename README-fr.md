# eqasim-sa

[English](README.md) | **Français**

`eqasim-sa` planifie, exécute et analyse des campagnes d'analyse de sensibilité
pour un pipeline eqasim piloté par synpp. Son vocabulaire actuel est Grid, OAT
discret, Morris canonique et Sobol. Il fournit des plans déterministes, une
reprise par run, des indicateurs calculés sur les artefacts Parquet et des
backends local et SLURM.

Tous les starters générés, dont calibration, Grid, OAT discret et Sobol, sont
des démonstrations : ils
valident la configuration, l'exécution, le stockage et la plomberie d'analyse,
mais ne peuvent soutenir aucune revendication inférentielle ou décisionnelle.
Aucun starter Morris canonique n'est généré : ses revendications scientifiques,
son modèle d'incertitude, son estimand, son budget, ses critères de convergence
et ses contrôles de robustesse doivent être propres à l'étude. L'ancien
`method: morris` reste lisible uniquement comme provenance de démonstration.

Les exemples Grid et Sobol générés font varier le sélecteur scalaire
`matching_age_scheme` d'eqasim-france ; l'OAT discret garde ce facteur catégoriel fixe,
et `analysis_age_scheme` reste fixe pour comparer les distributions de sortie.
L'OAT discret utilise des niveaux numériques adjacents et des effets normalisés
locaux ; ce n'est pas Morris canonique. Morris canonique est pris en charge
uniquement pour le criblage scientifique, pas pour FP ni FF.

Le paquet cible exactement Python 3.13 (`>=3.13,<3.14`). Depuis ce répertoire :

```bash
uv sync --extra test --locked
uv run --locked eqasim-sa --help
```

## Initialiser une étude

Depuis la racine qui contient `eqasim-sa/`, `eqasim-france/` et `eqasim-data/` :

```bash
ROOT="$(pwd -P)"
SA=(uv run --project "$ROOT/eqasim-sa" --locked eqasim-sa)

"${SA[@]}" init \
  --project-name mon-etude \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
cd "$ROOT/mon-etude"
```

`init` génère uniquement un projet modèle et `manifest.json`, qui hache l'arbre
des données. Il ne crée aucun plan et n'exécute ni synpp, ni campagne, ni
analyse.

## Workflow standard

```bash
# Produire d'abord les observations de calibration des ressources.
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml

# Puis préparer, vérifier, estimer, exécuter et analyser une cible.
"${SA[@]}" plan --meta-config configs/01-grid.yml
"${SA[@]}" check --meta-config configs/01-grid.yml
"${SA[@]}" estimate --meta-config configs/01-grid.yml
"${SA[@]}" run --meta-config configs/01-grid.yml
"${SA[@]}" analyze --meta-config configs/01-grid.yml
```

La calibration concerne le temps, le stockage durable et le cache worker. Elle
ne calibre pas les paramètres scientifiques d'eqasim.

## Documentation

- [Index de la documentation](docs-fr/index.md)
- [Démarrage](docs-fr/getting-started.md)
- [Référence CLI](docs-fr/cli-reference.md)
- [Référence YAML](docs-fr/yaml-reference.md)
- [Paramètres et méthodes](docs-fr/parameters-and-methods.md)
- [Workflow et résultats](docs-fr/workflow-and-results.md)
- [Dépannage](docs-fr/troubleshooting.md)
- [Glossaire](docs-fr/glossary.md)
- [Guide de développement](docs-fr/development-guide.md)
