# eqasim-sa

[English](README.md) | **Français**

`eqasim-sa` planifie, exécute et analyse des campagnes d'analyse de sensibilité
pour un pipeline eqasim piloté par synpp. Son vocabulaire actuel est Grid, OAT
discret, Morris canonique, Sobol de démonstration et Sobol multi-scramble. Il fournit des plans déterministes, une
reprise par run, une archive à l'octet près des fichiers de sortie que vous
déclarez, des indicateurs calculés sur ces fichiers ou par vos propres scripts
d'analyse (`tv_distance`, `share`, `value`) et des backends local et SLURM.

Il fonctionne avec eqasim officiel, non modifié : indiquez-lui un dossier eqasim
(un clone, une copie ou une archive) et le fichier de configuration de votre
étude. Git n'est pas nécessaire ; le code d'eqasim est identifié par une
empreinte de son contenu, et synpp exécute la liste `run:` de votre
configuration eqasim telle quelle.

Tous les starters générés, dont calibration, Grid, OAT discret et Sobol, sont
des démonstrations : ils
valident la configuration, l'exécution, le stockage et la plomberie d'analyse,
mais ne peuvent soutenir aucune revendication inférentielle ou décisionnelle.
Aucun starter Morris canonique n'est généré : ses revendications scientifiques,
son modèle d'incertitude, son estimand, son budget, ses critères de convergence
et ses contrôles de robustesse doivent être propres à l'étude. Le
fichier généré `03-sobol.yml` est une démonstration : son
scramble unique et son bootstrap de lignes sont non inférentiels.

Les starters générés ne font varier que `sampling_rate`, un paramètre de la
configuration officielle d'eqasim. L'OAT discret utilise des niveaux numériques adjacents et des effets normalisés
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
génère pas. Le support fini de son facteur `seed`, écrit dans `experiment.seeds`
avec un `experiment.seeds_provenance` obligatoire, est un facteur de nuisance
déclaré, ni une entrée contrôlable ni un modèle du bruit stochastique intrinsèque.
Ses indices restent diagnostiques, mais il est non actionnable et exclu des rangs
FP/FF et de la classification de tolérance. Les tables de
convergence apportent des éléments aux tailles déclarées sans déclarer
automatiquement les critères d'arrêt satisfaits.

Le paquet cible exactement Python 3.13 (`>=3.13,<3.14`). Windows 10+, macOS 13+
et Linux sont pris en charge nativement, avec une installation par `uv` sans
droits administrateur (sous Windows :
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`) ;
la vérification sous Windows et macOS est en attente, voir la
[vérification des plateformes](docs/platform-verification.md). Sous Windows sans
chemins longs, un run eqasim réel exige qu'un administrateur les active une fois
(le cache d'eqasim dépasse à lui seul 260 caractères). Le backend `local`
fonctionne sur les trois ; le backend `slurm` exige Linux. Une campagne
s'exécute entièrement sur une seule machine ; l'analyse peut se faire sur une
autre. Voir le [démarrage](docs-fr/getting-started.md) et le
[dépannage](docs-fr/troubleshooting.md) (longueur de chemin Windows, fins de
ligne). Les exemples shell ci-dessous sont en Bash ; sous Windows, lancez-les
depuis Git Bash. Depuis ce répertoire :

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
analyse. Les modèles utilisent le `config.yml` officiel du dossier eqasim ;
pour utiliser votre propre copie, faites-la dans le dossier eqasim
(`cp config.yml my_config.yml`, ou `cp config_nantes.yml config_nantes_sa.yml`
pour Nantes : eqasim fournit lui-même `config_nantes.yml`) et ajoutez
`--model-config my_config.yml`.

Avant de planifier, ajoutez `analysis.synthesis.population` à la liste `run:` de
cette configuration eqasim : les sorties `age`, `distance` et `nbr_vehicle`
comparées par les modèles sont écrites par cette étape, et le `config.yml`
officiel ne liste que `synthesis.output`. Sinon, eqasim-sa refuse de charger un
modèle.

## Le fichier de configuration

Chaque expérience tient dans un court fichier YAML. Le fichier généré
`configs/01-grid.yml` commence par ce que vous choisissez : la méthode et ses
graines, les valeurs de paramètres à essayer, les fichiers de sortie à
conserver, et les sorties à comparer aux données de référence.

```yaml
campaign: mon-etude-grid
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
```

Sous une ligne de commentaire, `init` a rempli les chemins `model` et
`execution` propres à la machine. Toute clé omise prend une valeur par défaut :
les sorties eqasim connues comme `persons` ou `age` ont un fichier par défaut,
le dossier de campagne est `../campaigns/<campaign>`, et sans bloc `indicators`
la distance de variation totale est calculée pour chaque sortie et chacune de
ses références. Les grandeurs personnalisées (une distance moyenne
domicile-travail, une part modale) viennent de courts
[scripts d'analyse](docs-fr/analysis-scripts.md) en Python qui lisent les
fichiers archivés de chaque run ; on peut les ajouter après les runs, sans
relancer eqasim. Pour voir la configuration complète, valeurs par défaut
comprises :

```bash
"${SA[@]}" show-config configs/01-grid.yml
```

Voir la [référence YAML](docs-fr/yaml-reference.md) pour toutes les clés.

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
"${SA[@]}" plot --meta-config configs/01-grid.yml
```

La calibration concerne le temps, le stockage durable et le cache worker. Elle
ne calibre pas les paramètres scientifiques d'eqasim.

`analyze` peut être relancé à tout moment. Après une campagne sur un
supercalculateur, rapatriez le dossier de campagne, ajoutez des scripts
d'analyse et les indicateurs qui les lisent, puis lancez `analyze` sur votre
ordinateur : le plan et les archives des runs ne sont jamais modifiés. Voir
[Écrire un script d'analyse](docs-fr/analysis-scripts.md).

`plot` dessine, à partir de l'analyse publiée, uniquement les figures valables
pour la méthode et le statut d'inférence de la campagne, dans
`figures/<generationId>/`. Voir [Figures](docs-fr/workflow-and-results.md#figures).

## Configurations structurées de matching

Grid peut comparer des listes ordonnées `matching_attributes` nommées tout en
conservant la liste résolue dans l'identité et l'exécution du run. Votre
configuration eqasim doit déjà contenir cette clé active sous forme de liste ; utilisez un nouveau
nom de campagne pour la cible dérivée. Consultez le
[tutoriel pratique](docs-fr/structured-configurations-tutorial.md).

## Documentation

- [Registre du protocole scientifique nantais](protocols/nantes-scientific/README.md) (rédigé pour l'étude, non généré par `init`)
- [Index de la documentation](docs-fr/index.md)
- Guides des méthodes : [Grid](docs-fr/what-when-how-grid.md), [OAT discret](docs-fr/what-when-how-discrete_oat.md), [Morris canonique](docs-fr/what-when-how-canonical_morris.md), [Sobol de démonstration](docs-fr/what-when-how-sobol.md) et [Sobol multi-scramble](docs-fr/what-when-how-multiscramble_sobol.md)
- [Démarrage](docs-fr/getting-started.md)
- [Tutoriel des configurations structurées nommées](docs-fr/structured-configurations-tutorial.md)
- [Référence CLI](docs-fr/cli-reference.md)
- [Référence YAML](docs-fr/yaml-reference.md)
- [Écrire un script d'analyse](docs-fr/analysis-scripts.md)
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
