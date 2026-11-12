# eqasim-sa

[English](README.md) | **Français**

`eqasim-sa` planifie, exécute et analyse des campagnes d'analyse de sensibilité
pour un pipeline eqasim piloté par synpp. Son vocabulaire actuel est Grid, OAT
discret, Morris canonique, Sobol historique et Sobol multi-scramble. Il fournit des plans déterministes, une
reprise par run, des indicateurs calculés sur les artefacts Parquet et des
backends local et SLURM.

Tous les starters générés, dont calibration, Grid, OAT discret et Sobol, sont
des démonstrations : ils
valident la configuration, l'exécution, le stockage et la plomberie d'analyse,
mais ne peuvent soutenir aucune revendication inférentielle ou décisionnelle.
Aucun starter Morris canonique n'est généré : ses revendications scientifiques,
son modèle d'incertitude, son estimand, son budget, ses critères de convergence
et ses contrôles de robustesse doivent être propres à l'étude. L'ancien
`method: morris` reste lisible uniquement comme provenance de démonstration. Le
fichier généré `03-sobol.yml` reste lui aussi une démonstration historique : son
scramble unique et son bootstrap de lignes sont non inférentiels. Ses octets de
plan, son schéma, ses sorties et son comportement restent inchangés.

Les exemples Grid et Sobol générés font varier le sélecteur scalaire
`matching_age_scheme` d'eqasim-france ; l'OAT discret garde ce facteur catégoriel fixe,
et `analysis_age_scheme` reste fixe pour comparer les distributions de sortie.
L'OAT discret utilise des niveaux numériques adjacents et des effets normalisés
locaux ; ce n'est pas Morris canonique. Morris canonique est pris en charge
uniquement pour le criblage scientifique, pas pour FP ni FF. Il cible la moyenne
conditionnelle sur les seeds du simulateur. Le bloc `stochastic` déclare une
allocation pilote ou production et des nombres aléatoires communs dans chaque
trajectoire. Le pilote est une campagne immuable et séparée : l'analyse publie
`noise-floor.parquet` et `replication-allocation.json`, qui recommande une
nouvelle campagne/racine de production. La production conserve cette
recommandation comme provenance et ne prolonge jamais le plan pilote.

La méthode `multiscramble_sobol`, rédigée pour une étude, est exclusivement
scientifique. Elle exige une dépendance `independent`, un objectif `fp` ou `ff`
et au moins quatre seeds explicites de scrambles QMC indépendants. `init` ne la
génère pas. Le support fini de `parameters.seed` est un facteur de nuisance
déclaré, ni une entrée contrôlable ni un modèle du bruit stochastique intrinsèque.
Ses indices restent diagnostiques, mais il est non actionnable et exclu des rangs
FP/FF et de la classification de tolérance. Les tables de
convergence apportent des éléments aux tailles déclarées sans déclarer
automatiquement les critères d'arrêt satisfaits.

Le paquet cible exactement Python 3.13 (`>=3.13,<3.14`) et fonctionne
nativement sous Windows 10+, macOS 13+ et Linux, installé avec `uv` et sans
droits administrateur (sous Windows :
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`).
Le backend `local` fonctionne sur les trois ; le backend `slurm` exige Linux. Une
campagne s'exécute entièrement sur une seule machine ; l'analyse peut se faire
sur une autre. Voir le [démarrage](docs-fr/getting-started.md) et le
[dépannage](docs-fr/troubleshooting.md) (longueur de chemin Windows, fins de
ligne). Depuis ce répertoire :

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

## Configurations structurées de matching

Grid peut comparer des listes ordonnées `matching_attributes` nommées tout en
conservant la liste résolue dans l'identité et l'exécution du run. Le YAML modèle
doit déjà contenir cette clé active sous forme de liste; utilisez une nouvelle
campagne/racine pour la cible dérivée. Consultez le
[tutoriel pratique](docs-fr/structured-configurations-tutorial.md).

## Documentation

- [Registre du protocole scientifique nantais](protocols/nantes-scientific/README.md) (rédigé pour l'étude, non généré par `init`)
- [Index de la documentation](docs-fr/index.md)
- Guides des méthodes : [Grid](docs-fr/what-when-how-grid.md), [Morris historique](docs-fr/what-when-how-morris.md), [OAT discret](docs-fr/what-when-how-discrete_oat.md), [Morris canonique](docs-fr/what-when-how-canonical_morris.md), [Sobol historique](docs-fr/what-when-how-sobol.md) et [Sobol multi-scramble](docs-fr/what-when-how-multiscramble_sobol.md)
- [Démarrage](docs-fr/getting-started.md)
- [Tutoriel des configurations structurées nommées](docs-fr/structured-configurations-tutorial.md)
- [Référence CLI](docs-fr/cli-reference.md)
- [Référence YAML](docs-fr/yaml-reference.md)
- [Paramètres et méthodes](docs-fr/parameters-and-methods.md)
- [Workflow et résultats](docs-fr/workflow-and-results.md)
- [Dépannage](docs-fr/troubleshooting.md)
- [Glossaire](docs-fr/glossary.md)
- [Guide de développement](docs-fr/development-guide.md)

## Contribuer

Les contributions sont les bienvenues. Consultez
[CONTRIBUTING-fr.md](CONTRIBUTING-fr.md) pour le workflow de développement, les
commandes de validation et les règles du contrat scientifique.

## Licence

`eqasim-sa` est distribué sous la [licence publique générale GNU version 2.0
uniquement](LICENSE).
