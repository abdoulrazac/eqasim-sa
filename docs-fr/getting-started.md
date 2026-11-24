# Démarrage

[English](../docs/getting-started.md) | **Français**

## Prérequis

- Windows 10 ou ultérieur, macOS 13 ou ultérieur, ou Linux (pris en charge ; la suite est testée sous Linux, Python 3.13 : 1558 réussis, 4 ignorés ; le flux d'intégration continue GitHub Actions `.github/workflows/tests.yml` l'exécute sous Ubuntu, macOS et Windows, et aucun résultat Windows ou macOS n'est revendiqué tant que l'intégration continue n'a pas tourné). Le backend `local` se comporte de la même façon sur les trois ; le backend `slurm` exige Linux (voir le [dépannage](troubleshooting.md#backend-slurm-sous-windows-ou-macos)). Aucun droit administrateur n'est nécessaire, sauf sous Windows sans chemins longs, où un run eqasim réel exige qu'un administrateur les active une fois (voir [Scratch court sous Windows](#scratch-court-sous-windows)).
- Campagnes, scratch et données sur un disque local NTFS, APFS, ext4 ou xfs, ou sur le système de fichiers partagé du cluster. Les volumes FAT/exFAT et les partages SMB ne sont pas pris en charge pour les campagnes (non détectés : eqasim-sa ne les refuse pas, vérifiez donc où se trouvent vos chemins).
- Python 3.13 exactement dans la plage déclarée `>=3.13,<3.14`.
- `uv` disponible. Git n'est pas nécessaire.
- Un dossier eqasim (un clone, une copie ou une archive décompressée) contenant son fichier de configuration (par défaut `config.yml`) et un `uv.lock`.
- Un répertoire de données existant sans liens symboliques pour `init`.
- L'environnement synpp requis par eqasim. `synpp` n'est pas une dépendance Python d'`eqasim-sa`.

## Installer uv sans droits administrateur

Linux et macOS :

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell) :

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`uv` s'installe dans votre profil utilisateur et télécharge lui-même Python 3.13
au besoin. Les exemples de ce guide utilisent la syntaxe Bash. Sous Windows,
lancez-les depuis Git Bash, ou en PowerShell écrivez la commande complète
`uv run --project <chemin> --locked eqasim-sa` à la place de `"${SA[@]}"`.

## Installer avec uv

Récupérez eqasim-sa en clonant son dépôt à l'étiquette d'une version, par
exemple `git clone --branch v0.4.1 https://github.com/abdoulrazac/eqasim-sa.git`.
Sans Git, téléchargez l'archive « Source code » d'une version depuis la
[page des versions GitHub](https://github.com/abdoulrazac/eqasim-sa/releases)
et décompressez-la. Puis, à la racine qui la contient :

```bash
ROOT="$PWD"
uv sync --project "$ROOT/eqasim-sa" --locked
uv sync --project "$ROOT/eqasim-france" --locked
SA=(uv run --project "$ROOT/eqasim-sa" --locked eqasim-sa)
```

Pour vérifier l'installation, `"${SA[@]}" --version` affiche la version
installée, par exemple `eqasim-sa 0.4.1`. `uv sync --locked` installe
exactement les versions du `uv.lock` d'eqasim-sa.

Le lockfile d'`eqasim-sa` fixe l'environnement d'exécution d'eqasim-sa. Le lockfile référencé
dans une méta-configuration entre, lui, dans le contexte scientifique scellé.

Les dossiers `eqasim-sa/`, `eqasim-france/` et `eqasim-data/` côte à côte sous
une même racine ne sont qu'un exemple. Chacun peut se trouver n'importe où,
tant qu'ils ne sont pas imbriqués les uns dans les autres (`init` refuse un
projet dans le dossier eqasim ou l'inverse, un scratch dans le dossier eqasim
et un projet dans son scratch) :
donnez des chemins absolus à `--project`, `--model-root`, `--data-path`,
`--directory` et `--scratch`. `init` écrit les chemins des *fichiers de
départ* (les quatre méta-configurations de démonstration qu'il crée dans
`configs/`, appelées *starters* dans la documentation anglaise) relativement à
leur dossier `configs/` (en absolu sous Windows quand le dossier est sur un
autre lecteur) ; si vous déplacez ensuite le projet sans ces dossiers,
modifiez les chemins sous le commentaire « Machine-specific values » de
chaque fichier de départ.

## Initialiser un projet

```bash
"${SA[@]}" init \
  --project-name mon-analyse-sa \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
```

`--model-config` désigne la configuration eqasim de votre étude, relative à
`--model-root` ou absolue ; elle vaut par défaut `config.yml`, le fichier
officiel pour l'Île-de-France. Travaillez sur une copie faite dans le dossier
eqasim, pour laisser les fichiers officiels intacts :
`cp config.yml my_config.yml` en général, ou, pour Nantes,
`cp config_nantes.yml config_nantes_sa.yml` (eqasim fournit lui-même
`config_nantes.yml` ; ne l'écrasez pas). Ajoutez ensuite `--model-config my_config.yml` (ou
`--model-config config_nantes_sa.yml`) ; les fichiers de départ contiennent alors
`config: my_config.yml` (ou `config: config_nantes_sa.yml`).

`init` est strictement un générateur atomique de projet. Il rend quatre YAML,
un README et un `manifest.json` contenant la taille et le SHA-256 de chaque
fichier de données. Il ne lance aucun sous-processus, ne vérifie pas
l'environnement synpp, ne planifie rien et n'exécute aucune étape du pipeline.

```text
mon-analyse-sa/
├── .gitattributes
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

`init` écrit aussi un `.gitattributes` qui impose les fins de ligne LF pour
`*.yml`, `*.yaml`, `*.json` et `*.lock`, afin qu'une conversion CRLF lors d'une
extraction Git sous Windows ne modifie pas les entrées scellées. Il n'écrase jamais un
`.gitattributes` existant ; s'il manque, il est recréé et signalé sous
`restored`.

Une relance identique renvoie `status: unchanged` sans rehacher les données.
`--regenerate-data-manifest` remplace atomiquement le manifeste local, mais ne
modifie ni les YAML, ni les plans, ni les résultats ; elle renvoie
`status: initialized`. Les deux fonctionnent après avoir planifié, lancé et
analysé les fichiers de départ : avant d'écrire quoi que ce soit, `init` accepte les
fichiers générés, les dossiers de campagne des fichiers de départ
(`campaigns/mon-analyse-sa-calibration`, `-grid`, `-discrete_oat`, `-sobol`)
avec ce qu'eqasim-sa y a écrit (`figures/` compris), et `configs/analyses/`
pour vos scripts d'analyse. Un fichier de départ (par exemple complété par `analyses`) ou
un README modifié est conservé tel quel et listé dans `kept` ; un fichier
manquant est recréé et listé dans `restored`. Seul le changement d'une valeur
remplie par `init` (`model.root`, `model.config`, `model.data_manifest`,
`execution.scratch`, `execution.invocation`,
`execution.machine_options.data_path`, `campaign`, `campaign_root`) est
refusé, en la nommant : `init inputs changed: model.root in
configs/01-grid.yml`. Ces valeurs sont comparées telles qu'écrites (après les
valeurs par défaut) : un `model.root` absolu qui désigne le même dossier compte
comme un changement. Un autre fichier dans `configs/` ou un dossier de
campagne à vous est aussi refusé. La
[référence CLI](cli-reference.md#relancer-init-sur-un-projet-existant) donne
les règles exactes. Il n'est jamais nécessaire de relancer `init` pour
continuer à travailler.

Après un changement de l'arbre de données, rafraîchissez le manifeste avec
`--regenerate-data-manifest` : une relance identique ne rehache pas les
données, et seule cette option détecte un changement de données. Si les
données ont changé, une campagne planifiée
avant la régénération ne
correspond plus à son plan : `run` refuse de la reprendre avec `data manifest
changed since plan (dataManifestSha256)`. Conservez l'ancien état et utilisez
de nouvelles racines de campagne ou un nouveau projet.

## Commande eqasim-sa du projet généré

Le README généré définit un tableau Bash :

```bash
SA=(...)
```

Depuis une copie des sources, le rendu utilise
`uv run --project /chemin/vers/eqasim-sa --locked eqasim-sa`. Dans un paquet
installé sans `pyproject.toml` source détectable, il utilise `eqasim-sa`. Toujours
invoquer le tableau avec `"${SA[@]}"` pour préserver les arguments.

## Scratch court sous Windows

Windows limite les chemins à 260 caractères sauf si les chemins longs sont
activés. Le cache d'eqasim crée déjà des chemins relatifs allant jusqu'à environ
231 caractères : sous Windows sans chemins longs, aucun `execution.scratch` ne
convient à un run eqasim réel, et un administrateur doit activer une fois les
chemins longs Windows (`LongPathsEnabled`), en dehors d'eqasim-sa, tant
qu'eqasim ne crée pas de chemins plus courts. Utilisez un `execution.scratch`
court, comme `C:\esa`, et lancez `check` avant `run` : il signale ce cas avant
tout run long. Voir le
[dépannage](troubleshooting.md#le-contrôle-path_length-échoue-sous-windows).

## Lire une configuration générée

Chaque fichier de `configs/` est court : le nom de la campagne, la méthode et ses
graines, les valeurs de paramètres à essayer, les sorties du modèle à comparer
aux données de référence et, sous une ligne de commentaire, les chemins
qu'`init` a remplis pour cette machine. Tout le reste prend une valeur par
défaut. La [référence YAML](yaml-reference.md#un-premier-fichier) lit le fichier de départ
Grid ligne par ligne. Pour voir la configuration complète qu'eqasim-sa
utilisera, toutes valeurs par défaut écrites :

```bash
"${SA[@]}" show-config configs/01-grid.yml
```

`show-config` affiche du YAML, pas du JSON. Si le fichier est invalide, la
commande écrit l'erreur sur la sortie d'erreur et se termine avec le code 1.

## Préparer votre configuration eqasim

Les fichiers de départ fonctionnent avec eqasim officiel, non modifié. Avant le premier
`plan`, vérifiez deux points dans la configuration eqasim désignée par
`model.config` :

- Sa liste `run:` doit contenir `analysis.synthesis.population`. Les fichiers de départ
  comparent les distributions `age`, `distance` et `nbr_vehicle` aux données du
  recensement et des enquêtes, et ces fichiers sont écrits par cette étape
  d'eqasim. Le `config.yml` officiel ne liste que `synthesis.output` ; ajoutez
  la seconde étape :

  ```yaml
  run:
    - synthesis.output
    - analysis.synthesis.population
  ```

  eqasim-sa exécute `run:` exactement tel qu'il est écrit. Si l'étape manque,
  le chargement s'arrête avec `output 'age' is written by eqasim stage
  'analysis.synthesis.population', which is not in run: of ...`.
- Chaque paramètre que vous faites varier doit être écrit dans sa section
  `config:`. Les fichiers de départ ne font varier que `sampling_rate`, que la
  configuration officielle contient. Pour en faire varier un autre, par exemple
  `matching_minimum_age`, écrivez-le d'abord dans `config:`.

Modifier ce fichier après le scellement d'un plan change l'identité de la
campagne : faites ces modifications avant de planifier, ou utilisez ensuite une
nouvelle campagne.

## Première calibration des ressources

Depuis `mon-analyse-sa/`, sceller puis exécuter la calibration :

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml
```

Le premier `run` fournit les observations dont `check` et `estimate` ont besoin :
durée, octets durables et taille maximale du cache. Un contrôle statique peut
être lancé ensuite, mais il n'est pas un prérequis au démarrage :

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

En option, une fois `analyze` publié, dessiner les figures :

```bash
"${SA[@]}" plot --meta-config "$TARGET"
```

`plot` écrit les figures PNG valides pour la méthode, plus un index `figures.json`, dans `figures/<generationId>/` ; il ne touche pas à `analysis/`. Voir [Figures](workflow-and-results.md#figures).

`check` sans `--smoke` valide la méta-configuration, le plan, l'environnement et
l'espace estimé. `check --smoke` ajoute deux vrais runs temporaires et doit donc
être réservé à une validation explicite du pipeline. Il part de la
configuration du premier run du plan, avec `execution.machine_options`
appliqué comme pour `run` (`execution.machine_options.data_path` est donc
utilisé), puis force `sampling_rate: 0.001` et les deux premières seeds
distinctes.

Les fichiers de départ fournis sont de petites campagnes de démonstration. Leurs tailles ne
constituent pas une recommandation scientifique et `init` ne génère délibérément
aucun fichier de départ Morris canonique. Consultez [Paramètres et méthodes](parameters-and-methods.md)
avant d'étendre les domaines ou de rédiger une campagne scientifique propre à l'étude.
Utilisez les guides courts de [Grid](what-when-how-grid.md),
[OAT discret](what-when-how-discrete_oat.md),
[Morris canonique](what-when-how-canonical_morris.md), [Sobol de démonstration](what-when-how-sobol.md)
et [Sobol multi-scramble](what-when-how-multiscramble_sobol.md) pour choisir la
bonne méthode et la limite des revendications.

## Première cible structurée

Pour comparer des listes ordonnées `matching_attributes` nommées, écrivez
d'abord une clé active sous forme de liste dans votre configuration eqasim, puis
dérivez une configuration Grid avec une nouvelle campagne/racine. Le
[tutoriel des configurations structurées](structured-configurations-tutorial.md)
couvre les six runs, les métadonnées du plan, l'inspection de l'analyse et les
limites scientifiques. `init` ne modifie pas le YAML modèle et ne génère pas
automatiquement cette cible.

## Points de contrôle minimaux des fichiers de départ générés

Les commandes `plan` renvoient un objet JSON avec `status`, `designPositions` et
`uniqueRuns`. Avec les fichiers de départ non modifiés issus d'`init`, vérifiez :

| Étape | JSON minimal à la première exécution | Fichiers attendus après `run`/`analyze` |
| --- | --- | --- |
| Calibration | `plan` : `created`, 3 positions, 3 runs uniques ; `run` : `completed`, 3 complétés | `campaigns/mon-analyse-sa-calibration/plan.parquet`, 3 marqueurs sous `runs/`, 3 archives de huit fichiers chacune (`persons.csv`, …, `activities_spatial.gpkg`, `age.csv`, `distance.csv`, `nbr_vehicle.csv`) |
| Grid | `plan` : `created`, 4 positions, 4 runs uniques ; `run` : 4 complétés ; `analyze` : `completed` | `analysis-generation.json` ; génération désignée : `indicators.parquet`, `replication-dispersion.parquet`, `output-summary.parquet`, `governed-report.json` |
| OAT discret | `plan` : `created`, 8 positions, 6 runs uniques ; `run` : 6 complétés ; `analyze` : sensibilité `discrete_oat` | `analysis-generation.json` ; génération désignée : `indicators.parquet`, `replication-dispersion.parquet`, `discrete-oat-effects.parquet`, `output-summary.parquet`, `governed-report.json` |
| Sobol | `plan` : `created`, 48 positions, 4 runs uniques ; `run` : 4 complétés ; `analyze` : sensibilité `sobol` | `analysis-generation.json` ; génération désignée : `indicators.parquet`, `sobol-indices.parquet`, `output-summary.parquet`, `governed-report.json` |

Une relance identique de `plan` renvoie `unchanged`. Une campagne déjà complète
renvoie pour `run` `status: completed` et `total: 0`.

## Points à vérifier

- Les quatre plans correspondent aux cardinalités attendues ou à votre design documenté.
- Chaque `runId` attendu possède un marqueur valide et une archive lisible.
- `analyze` annonce `status: completed` et le taux de complétude attendu.
- Les noms de colonnes des fichiers CSV archivés correspondent aux déclarations `category`, `simulated` et `references` sous `outputs`.
- Les sorties de démonstration Grid, OAT discret et Sobol ont été inspectées, pas seulement créées.
- Le Sobol généré est une démonstration non inférentielle. Une campagne scientifique
  multi-scramble doit être rédigée séparément ; aucun fichier de départ n'est fourni.
- Le modèle, les données, le lockfile et la version d'`eqasim-sa` sont archivés ou traçables.

Voir aussi le [glossaire](glossary.md).
