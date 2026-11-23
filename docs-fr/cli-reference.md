# Référence CLI

[English](../docs/cli-reference.md) | [Français](cli-reference.md)

Lorsqu'une sous-commande publique atteint son traitement applicatif, elle écrit
normalement un objet JSON sur la sortie standard. Les avertissements, notamment
la capacité Sobol, vont sur la sortie d'erreur. `--help` et l'absence de
sous-commande affichent du texte, pas du JSON. Une erreur de syntaxe `argparse`
affiche l'usage et renvoie 2. Une interruption peut terminer à 130 sans garantir
qu'un objet JSON complet ait été écrit. `show-config` et `plot` font exception :
`show-config` affiche du YAML, jamais du JSON ; `plot` affiche du JSON en cas de
succès mais écrit ses erreurs en texte brut sur la sortie d'erreur.

`eqasim-sa --version` affiche la version installée en texte brut (par exemple
`eqasim-sa 0.3.0`) et renvoie 0.

## `init`

```text
eqasim-sa init --project-name NAME [options]
```

| Option | Requise | Défaut | Contrat |
| --- | --- | --- | --- |
| `--project-name NAME` | oui | aucun | segment sûr de 1 à 64 caractères |
| `--directory PATH` | non | `./NAME` | destination du projet |
| `--model-root PATH` | non | `./eqasim-france` | dossier eqasim (Git n'est pas nécessaire) |
| `--model-config FILE` | non | `config.yml` | configuration eqasim, relative à `--model-root` ou absolue ; écrite comme `model.config` dans les modèles et nommée dans le README généré |
| `--data-path PATH` | non | `./eqasim-data` | arbre de données à hacher |
| `--scratch PATH` | non | `<directory>/scratch` | scratch local, créé si absent |
| `--invocation ARG` | non, répétable | `uv run --locked python -m synpp` | un argument par occurrence, dans l'ordre |
| `--regenerate-data-manifest` | non | faux | rehache et remplace uniquement `manifest.json` |

Statuts JSON : `initialized`, `unchanged` ou `failed`. Champs : `initVersion`,
`projectName`, `manifest`, `readme`, `metaConfigs`, `restored`,
`elapsedSeconds`, `error`. `elapsedSeconds` est la durée de la commande, aussi
dans un résultat `failed`. `restored` liste les fichiers générés qui manquaient
dans un projet existant et ont été recréés (aujourd'hui seulement
`.gitattributes`) ; un `.gitattributes` existant n'est jamais écrasé.
La commande est template-only : le hachage des données n'est pas une exécution
du pipeline.

### Relancer `init` sur un projet existant

Relancer `init` avec les mêmes options renvoie `unchanged` sans rehacher les
données. La commande vérifie d'abord, sans rien écrire, que le projet ne
contient que ce qu'ont écrit `init` et les commandes lancées sur les modèles :

- à la racine : `manifest.json`, `README.md`, `.gitattributes`, `configs/`,
  `campaigns/`, et le dossier de scratch s'il est dans le projet ;
- dans `configs/` : les quatre modèles, octet pour octet tels qu'`init` les
  écrirait, et éventuellement `configs/analyses/`, le dossier de vos scripts
  d'analyse (fichiers ordinaires et sous-dossiers, sans lien symbolique ; leur
  contenu n'est pas lu) ;
- dans `campaigns/` : seulement les dossiers de campagne des quatre modèles,
  nommés d'après leur `campaign` (`campaign_root` par défaut
  `../campaigns/<campaign>`) : `<projet>-calibration`, `<projet>-grid`,
  `<projet>-discrete_oat` et `<projet>-sobol`. Chacun ne peut contenir que ce
  qu'y écrivent `plan`, `run`, `analyze` et `plot` : `plan.parquet`, `runs/`,
  `archives/`, `logs/`, `.eqasim-sa/`, `analysis/`, `analyses-cache/`,
  `figures/` et les fichiers de résultats publiés.

Tout le reste arrête la relance avec `foreign content in init project: <entrée>`
ou `foreign content in init campaign: <entrée>` : un autre fichier ou dossier
dans `configs/` (par exemple une méta-configuration copiée), un dossier de
campagne à vous (par exemple `<projet>-grid-structured`) ou un lien
symbolique. Un modèle ou un README modifié est refusé en nommant le fichier,
par exemple `init would overwrite configs/01-grid.yml, which differs from what
init would write (file edited or init inputs changed)` ; le même refus apparaît
quand une option (`--model-config`, `--scratch`, `--invocation`, ...) diffère
du premier `init`. Ajouter `analyses` à `configs/01-grid.yml`, comme dans
[Écrire un script d'analyse](analysis-scripts.md), fait donc refuser les
relances suivantes. Relancer `init` n'est jamais nécessaire pour continuer :
`plan`, `run`, `check`, `estimate` et `analyze` n'en dépendent pas.

Avec `--regenerate-data-manifest`, le projet doit passer les mêmes contrôles ;
seul `manifest.json` est alors remplacé et le statut est `initialized`. Les
plans et résultats ne sont pas réécrits. Si les données ont changé, une
campagne planifiée avant refuse de reprendre : `run` s'arrête avec
`data manifest changed since plan (dataManifestSha256)`.

## `show-config`

```text
eqasim-sa show-config PATH
```

Affiche la méta-configuration complète, toutes valeurs par défaut appliquées
(voir [Valeurs par défaut et `show-config`](yaml-reference.md#valeurs-par-défaut-et-show-config)).
Le fichier est validé comme pour `plan` (dossier eqasim et configuration
trouvés, facteurs présents dans la configuration du modèle), mais les répertoires scratch et de
données n'ont pas besoin d'exister et aucun bloc `execution` n'est requis.
`execution.python` est écrit quand il se déduit de `execution.invocation`
(voir le tableau des valeurs par défaut).

En cas de succès, le YAML résolu est écrit sur la sortie standard et le code de
sortie vaut 0. Les chemins sont affichés tels qu'écrits dans le fichier : la
sortie est elle-même une méta-configuration valide. Pour un fichier invalide,
seul le message d'erreur est écrit sur la sortie d'erreur, rien n'est écrit sur
la sortie standard, et le code de sortie vaut 1. Contrairement aux autres
sous-commandes, il n'y a pas d'enveloppe JSON.

## `plan`

```text
eqasim-sa plan --meta-config PATH
```

Scelle le design déterministe dans `<campaign_root>/plan.parquet`. Une relance
identique renvoie `unchanged`; un plan différent sous la même racine échoue.
Champs JSON : `status`, `path`, `designPositions`, `uniqueRuns`, `contextHash`,
`error`.

## `check`

```text
eqasim-sa check --meta-config PATH \
  [--calibration-meta-config PATH ...] [--smoke]
```

`--calibration-meta-config` est répétable et s'ajoute aux chemins du bloc YAML
`calibration`; les doublons sont supprimés en conservant l'ordre.

Sans `--smoke`, les contrôles sont, dans cet ordre :

| Contrôle | Vérification | Statuts |
| --- | --- | --- |
| `metaConfig` | YAML, empreinte du code d'eqasim et contexte, facteurs, sorties déclarées et chemins | `passed`, `failed` |
| `sealedPlan` | égalité du plan avec le design recalculé | `passed`, `failed` |
| `uniqueConfigSeedPairs` | absence de doublons Grid ; validation scellée pour OAT discret, Morris canonique et Sobol | `passed`, `failed` |
| `eqasimEnvironment` | quand `execution.invocation` se termine par `-m synpp`, l'invocation où ces deux éléments sont remplacés par `-c "import synpp"` doit sortir avec le code 0 ; sinon la sonde est sautée, avec `note: synpp probe skipped: invocation does not end with -m synpp` | `passed`, `skipped`, `failed` |
| `backend` | le backend `slurm` ne tourne que sous Linux | `passed`, `failed` |
| `path_length` | chemin le plus long estimé sous `execution.scratch` : arborescence des workers d'eqasim-sa, arborescence de `check --smoke` et 240 caractères pour le cache propre d'eqasim. La limite est de 260 sous Windows (au-delà, le contrôle passe si les chemins longs Windows sont activés, sinon il échoue et indique la longueur maximale de `execution.scratch`), 1024 sous macOS et 4096 sous Linux (au-delà, `warning`). L'estimation figure dans `note` quand le contrôle passe, dans `error` sinon | `passed`, `warning`, `failed` |
| `same_machine` | les runs terminés de la campagne ont été exécutés sur cette machine avec le même exécutable, les mêmes variables d'environnement, la même configuration du modèle et la même installation d'eqasim-sa (voir [Dépannage](troubleshooting.md#campaign-runs-were-executed-on-another-machine)). `skipped` avant tout run terminé, pour un plan vide et pour le backend `slurm` (comparé sur le nœud de calcul) ; `note` dit lequel | `passed`, `skipped`, `failed` |
| `diskSpace` | espace durable et scratch d'après les observations disponibles | `passed`, `failed` |

`backend`, `path_length` et `same_machine` n'apparaissent que si la
méta-configuration se charge et possède un bloc `execution` ; `same_machine`
exige en plus un `sealedPlan` valide. `--smoke` ajoute un neuvième élément,
`smoke` (`passed` ou `failed`).

Chaque élément vaut `{name, status, error}`, plus `note` pour certains.
`error` n'est renseigné que si le statut est `failed` ou `warning`, et vaut
`null` sinon ; `note` porte l'information des éléments `passed` et `skipped`.
Seul `failed` rend le statut global `invalid` ; `warning` et `skipped` le
laissent `valid`.

Le contrôle statique a un budget global de 55 secondes et la sonde
d'environnement utilise au plus 20 secondes de ce budget.

`--smoke` n'est pas statique. Si les contrôles précédents passent, il exécute
deux runs eqasim temporaires réels avec les deux premières seeds distinctes et
`sampling_rate: 0.001`, valide leurs marqueurs et vérifie que chaque fichier de
sortie déclaré a été archivé, puis supprime le répertoire temporaire. Ce répertoire est `<execution.scratch>/.smoke-<8 hex>/` :
l'estimation `path_length` couvre donc aussi les runs du smoke. Il exige `sampling_rate` et deux seeds explicites. Il
n'applique pas `execution.machine_options` : sa configuration de départ est le
YAML modèle scellé. Ainsi, `execution.machine_options.data_path` est ignoré par
le smoke et
le `data_path` du modèle doit être utilisable depuis la machine qui lance le test.

Champs JSON : `status` (`valid` ou `invalid`), `checkVersion`,
`elapsedSeconds`, `checks`, les mesures de disque ci-dessous, puis `error`.
`elapsedSeconds` est la durée de toute la commande, runs du smoke compris.

| Champ | Signification |
| --- | --- |
| `estimatedDiskBytes` | taille durable de toute la campagne, comme `estimatedDiskBytes` d'`estimate` : taille de `plan.parquet` plus le nombre de runs uniques du plan, terminés compris, fois la taille durable moyenne d'un run observé |
| `requiredDiskBytes` | ce qui reste à écrire : `estimatedDiskBytes` moins ce que la campagne contient déjà (son `plan.parquet` et, pour chaque run terminé, son marqueur et ses fichiers archivés), au moins 0. Il est donc inférieur à `estimatedDiskBytes` dès que le plan est écrit |
| `freeDiskBytes` | espace libre sur le système de fichiers du dossier de campagne (ou de son plus proche parent existant) |
| `missingDiskBytes` | `requiredDiskBytes` moins `freeDiskBytes`, au moins 0 |
| `estimatedScratchBytes` | `execution.workers` fois le plus grand cache de worker observé |
| `freeScratchBytes` | espace libre sur le système de fichiers d'`execution.scratch` |
| `missingScratchBytes` | `estimatedScratchBytes` moins l'espace libre du scratch, au moins 0 ; quand le scratch et la campagne partagent un système de fichiers, c'est l'espace libre restant après `requiredDiskBytes` qui compte |

Les sept valent `null` quand ils ne peuvent pas être calculés, par exemple
avant tout run terminé (`diskSpace` échoue alors avec `provide a calibration
meta-config with a complete run`).

## `estimate`

```text
eqasim-sa estimate --meta-config PATH \
  [--calibration-meta-config PATH ...]
```

Exige un plan cible, un bloc `execution` et au moins un run complet parmi la
cible ou les campagnes de calibration. Pour chaque observation, `duration` et
`scratchBytes` viennent du marqueur, tandis que les octets durables sont mesurés
sur le marqueur et tous les fichiers de l'archive. Le calcul utilise la
moyenne des durées et tailles durables, et le maximum du cache par worker.

Champs JSON : `status`, `estimateVersion`, `method`, `formula`,
`formulaInputs`, `designPositions`, `runs`, `calibrationRuns`,
`meanDurationSeconds`, `meanDurableBytesPerRun`, `estimatedComputeHours`,
`estimatedDiskBytes`, `workers`, `observedScratchBytesPerWorker`,
`estimatedScratchBytes`, `error`.

`calibrationRuns` est le nombre d'observations derrière les moyennes : tous les
runs terminés des campagnes de calibration **et de la campagne cible
elle-même** (chaque dossier de campagne compté une fois). Ce ne sont donc pas
seulement les runs des campagnes de calibration : quand des runs de la cible se
terminent, il augmente et les moyennes les incluent. `runs` est le nombre de
runs uniques du plan cible, terminés compris ; `estimatedComputeHours` (`runs`
fois la durée moyenne) et `estimatedDiskBytes` (taille de `plan.parquet` plus
`runs` fois la taille durable moyenne) couvrent toute la campagne, pas
seulement ce qui reste. Pour l'espace encore nécessaire, voir
`requiredDiskBytes` de `check`.

`formula` donne le nombre de positions du design pour la méthode, et
`formulaInputs` ses valeurs ; `designPositions` est égal à la formule évaluée
sur ces valeurs :

| Méthode | `formula` | `formulaInputs` |
| --- | --- | --- |
| Grid | `R*prod(L)` | `R` : nombre de seeds ; `L` : nombre de valeurs de chaque paramètre, par exemple `{"R": 2, "L": {"sampling_rate": 2}}` |
| OAT discret, Morris canonique | `T(D+1)R` | `D` : paramètres ; `T` : trajectoires ; `R` : seeds |
| Sobol | `N(2D+2)` | `D` : paramètres plus la seed ; `N` : taille d'échantillon de base |
| Sobol multi-scramble | `S*N(2D+2)` | `D` : paramètres plus la seed ; `N` : plus grande taille d'échantillon de base ; `S` : seeds de scramble |

Les deux valent `null` seulement dans un résultat `failed`.

## `run`

```text
eqasim-sa run --meta-config PATH
```

Exige `execution` et un plan scellé. Exécute seulement les `runId` sans marqueur
valide. Une même campagne ne peut pas avoir deux invocations `run` concurrentes.
Champs JSON : `status`, `total`, `completed`, `skipped`, `failed`, `failures`,
`error`. Quand tout est déjà complet, `total` vaut 0.

## `analyze`

```text
eqasim-sa analyze --meta-config PATH
```

Recalcule les indicateurs depuis les fichiers de sortie archivés et publie
atomiquement les sorties d'analyse. Les colonnes des sorties (`category`,
`simulated`, `references`) et les indicateurs sont lus dans la
méta-configuration courante : on peut donc les corriger après les runs. Les
[scripts d'analyse](analysis-scripts.md) lus par les indicateurs s'exécutent
ici, sur des copies des fichiers archivés, avec un cache sous
`analyses-cache/`. Champs JSON : `status`, `path`, `runs`, `indicators`, `values`,
`completionRate`, `expected`, `completed`, `missing`, `failed`,
`dispersionPath`, `sensitivityMethod`, `sensitivityPath`, `convergencePath`,
`rankReversalsPath`, `byScramblePath`, `diagnosticsPath`, `noiseFloorPath`,
`replicationAllocationPath`, `outputSummaryPath`, `convergenceEvidencePath`,
`governedReportPath`, `inferenceStatus`, `analyses`, `warnings`, `error`.
`warnings` liste les entrées altérées du cache d'analyse qui ont été supprimées
puis recalculées (vide sinon). `analyses` associe à chaque analyse lue par un
indicateur `{"computed": n, "cached": m}` : le nombre de runs dont le résultat
a été calculé en exécutant le script, et le nombre relus depuis
`analyses-cache/`. Après la modification d'un seul script, seule cette analyse
a des résultats `computed`. Il vaut `{}` quand aucun indicateur ne lit de
sortie d'analyse. Dans un résultat en échec, `analyses` vaut `{}` et
`warnings` vaut `[]`.
`rankReversalsPath` est propre à `canonical_morris`; les deux chemins de scramble
sont propres à `multiscramble_sobol`.
`multiscramble_sobol` est accepté pour une charte scientifique après
reconstruction du plan scellé. En production canonique,
`replicationAllocationPath` est nul; pilote et production exposent respectivement
`pilot_only`, puis `unstable` ou `conditional`. Le JSON d'analyse expose en option
`byScramblePath`, `diagnosticsPath` et réutilise `convergencePath`. Les nouvelles
démonstrations générées utilisent `discrete_oat`.

## `plot`

```text
eqasim-sa plot --meta-config PATH [--figure NAME ...]
```

Dessine les figures valables pour la génération d'analyse publiée dans
`figures/<generationId>/` (PNG et `figures.json`), après avoir vérifié le sha256
de chaque sortie publiée lue. Elle exige un `analyze` préalable et n'écrit rien sous
`analysis/`. `--figure` est répétable et limite le dessin aux figures nommées ;
une figure qui n'est pas valable pour la méthode de la campagne et les tables
publiées est une erreur qui liste les figures valables. Le dossier de la
génération est reconstruit dans son ensemble à chaque exécution. Voir
[Figures](workflow-and-results.md#figures) pour les noms et la validité des
figures. La sortie standard est un objet JSON avec `status`, `generationId`,
`directory`, `figures` et `skipped`. En cas d'erreur, le message va sur stderr,
rien n'est écrit sur stdout et le code de sortie est 1.

## Commandes internes

Ces interfaces servent aux tests et à l'exécution d'un run isolé. Elles ne font
pas partie du workflow utilisateur normal.

### Interne : `fake-run`

```text
eqasim-sa fake-run --campaign-root PATH --campaign NAME --config JSON \
  --seed INT --expected-context-hash HASH --current-context-hash HASH \
  --workdir PATH
```

`--config` désigne un fichier JSON. Réponse : `runId`, `status`, `error`.

### Interne : `eqasim-run`

```text
eqasim-sa eqasim-run --campaign-root PATH --campaign NAME \
  --config-yaml PATH --eqasim-root PATH --invocation-json JSON \
  --scratch PATH --seed INT --expected-context-hash HASH \
  --current-context-hash HASH --outputs-json JSON
```

`--invocation-json` doit être un tableau JSON de chaînes. `--outputs-json` est
un objet JSON qui associe à chaque nom de sortie déclarée exactement
`{"file": ..., "separator": ...}` (le `file` peut utiliser `{prefix}`), par
exemple
`{"age": {"file": "analysis_population/{prefix}age.csv", "separator": ","}}` ;
le run archive exactement ces fichiers. Réponse : `runId`, `status`, `error`.

## Codes de sortie

| Code | Sens |
| ---: | --- |
| 0 | succès, aide sans commande, configuration affichée, plan inchangé ou run déjà complet |
| 1 | validation invalide (y compris un fichier invalide pour `show-config`), échec métier ou au moins un run échoué |
| 2 | arguments CLI invalides produits par `argparse` |
| 130 | interruption clavier gérée au niveau de l'application |

Les erreurs applicatives sont normalement encodées dans le JSON ; `show-config`
et `plot` les écrivent en texte brut sur la sortie d'erreur. `init` et
`check` masquent les chemins absolus dans leurs messages; cette garantie n'est
pas appliquée uniformément aux autres commandes.
