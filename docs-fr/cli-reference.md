# Référence CLI

[English](../docs/cli-reference.md) | [Français](cli-reference.md)

Lorsqu'une sous-commande publique atteint son traitement applicatif, elle écrit
normalement un objet JSON sur la sortie standard, sur une seule ligne à clés
triées (la même mise en forme pour toutes les commandes). Les avertissements, notamment
la capacité Sobol, vont sur la sortie d'erreur. `--help` et l'absence de
sous-commande affichent du texte, pas du JSON. Une erreur de syntaxe `argparse`
affiche l'usage et renvoie 2. Une interruption peut terminer à 130 sans garantir
qu'un objet JSON complet ait été écrit. `show-config` et `plot` font exception :
`show-config` affiche du YAML, jamais du JSON ; `plot` affiche du JSON en cas de
succès mais écrit ses erreurs en texte brut sur la sortie d'erreur.

`eqasim-sa --version` affiche la version installée en texte brut (par exemple
`eqasim-sa 0.4.1`) et renvoie 0.

## `init`

```text
eqasim-sa init --project-name NAME [options]
```

| Option | Requise | Défaut | Contrat |
| --- | --- | --- | --- |
| `--project-name NAME` | oui | aucun | segment sûr de 1 à 64 caractères |
| `--directory PATH` | non | `./NAME` | destination du projet |
| `--model-root PATH` | non | `./eqasim-france` | dossier eqasim (Git n'est pas nécessaire) |
| `--model-config FILE` | non | `config.yml` | configuration eqasim, relative à `--model-root` ou absolue ; écrite comme `model.config` dans les fichiers de départ et nommée dans le README généré |
| `--data-path PATH` | non | `./eqasim-data` | arbre de données à hacher |
| `--scratch PATH` | non | `<directory>/scratch` | scratch local, créé si absent |
| `--invocation ARG` | non, répétable | `uv run --locked python -m synpp` | un argument par occurrence, dans l'ordre |
| `--regenerate-data-manifest` | non | faux | rehache et remplace uniquement `manifest.json` |

Statuts JSON : `initialized`, `unchanged` ou `failed`. Champs : `initVersion`,
`projectName`, `manifest`, `readme`, `metaConfigs`, `restored`, `kept`,
`elapsedSeconds`, `error`. `elapsedSeconds` est la durée de la commande, aussi
dans un résultat `failed`. Lors d'une relance, `restored` liste les fichiers
générés qui manquaient et ont été recréés (`.gitattributes`, `README.md`, un
fichier de départ), et `kept` liste les fichiers de départ et le README modifiés, conservés tels
quels (chemins relatifs au projet ; les deux sont vides pour un nouveau
projet). Aucun fichier de départ, README ou `.gitattributes` n'est jamais
écrasé.
La commande ne fait qu'écrire le projet : le hachage des données n'est pas une
exécution du pipeline.

### Relancer `init` sur un projet existant

Relancer `init` avec les mêmes options renvoie `unchanged` sans rehacher les
données. Avant d'écrire quoi que ce soit, la commande vérifie le projet :

- à la racine : seulement `manifest.json`, `README.md`, `.gitattributes`,
  `configs/`, `campaigns/`, et le dossier de scratch s'il est dans le projet ;
- dans `configs/` : seulement les quatre fichiers de départ et, éventuellement,
  `configs/analyses/`, le dossier de vos scripts d'analyse (fichiers ordinaires
  et sous-dossiers, sans lien symbolique ; leur contenu n'est pas lu) ;
- les fichiers de départ : un fichier de départ modifié est conservé tel quel et listé dans `kept`,
  tant que les valeurs remplies par `init` sont toujours celles qu'`init`
  écrirait : `model.root`, `model.config`, `model.data_manifest`,
  `execution.scratch`, `execution.invocation` (absente vaut la valeur par
  défaut) et `execution.machine_options.data_path`, ainsi que `campaign` et
  `campaign_root` (absente vaut `../campaigns/<campaign>`). Ces valeurs sont
  comparées telles qu'écrites, une fois les valeurs par défaut appliquées, et
  non comme des chemins résolus : un `model.root` absolu qui désigne le même
  dossier est un changement. Ajouter `analyses`,
  des indicateurs, ou changer `workers` ou `seeds` est donc accepté. Si l'une
  de ces valeurs diffère, la relance s'arrête avec `init inputs changed: <clé>
  in configs/<fichier>`, par exemple `init inputs changed: model.root in
  configs/01-grid.yml` : c'est le cas quand une option (`--model-root`,
  `--model-config`, `--data-path`, `--scratch`, `--invocation`, `--directory`,
  `--project-name`) diffère du premier `init`, ou quand vous avez modifié cette
  valeur. Un fichier de départ illisible en YAML arrête la relance avec `init cannot
  parse configs/<fichier>: <erreur>`, où `<erreur>` donne la classe d'erreur de
  l'analyseur YAML (`ParserError`, `ScannerError`, ...), la ligne et la colonne
  quand l'analyseur les donne, et la cause (par exemple `ParserError at line 9,
  column 11: expected ',' or ']', but got '<stream end>'`). Un `README.md` modifié est conservé et listé
  dans `kept`. Un fichier de départ ou un README manquant est recréé et listé dans
  `restored` ;
- dans `campaigns/` : seulement les dossiers de campagne des quatre fichiers de départ,
  nommés d'après leur `campaign` : `<projet>-calibration`, `<projet>-grid`,
  `<projet>-discrete_oat` et `<projet>-sobol`. Chacun ne peut contenir que ce
  qu'y écrivent `plan`, `run`, `analyze` et `plot` : `plan.parquet`, `runs/`,
  `archives/`, `logs/`, `.eqasim-sa/`, `analysis/`, `analyses-cache/`,
  `figures/` et les fichiers de résultats publiés.

Tout le reste arrête la relance avec `foreign content in init project: <entrée>`
ou `foreign content in init campaign <campagne>: <entrée>` (l'entrée est
relative à ce dossier de campagne) : un autre fichier ou dossier
dans `configs/` (par exemple une méta-configuration copiée), un dossier de
campagne à vous (par exemple `<projet>-grid-structured`) ou un lien
symbolique (par exemple `foreign content in init project:
configs/analyses/lien.py`). Les fichiers de départ sont comparés avant les dossiers de campagne : une
`campaign` renommée est donc signalée par `init inputs changed: campaign in
...`. Relancer `init` n'est jamais nécessaire pour continuer : `plan`, `run`,
`check`, `estimate` et `analyze` n'en dépendent pas.

Après un changement de l'arbre de données, rafraîchissez `manifest.json` avec
`--regenerate-data-manifest`. Une relance sans cette option ne rehache pas les
données : elle renvoie `unchanged` même si les données ont changé, et seul
`--regenerate-data-manifest` détecte un changement de données. Avec cette
option, le projet doit passer les mêmes contrôles
(fichiers de départ conservés compris) ; seul `manifest.json` est alors remplacé et le
statut est `initialized`. Les plans et résultats ne sont pas réécrits. Si les
données ont changé, une campagne planifiée avant refuse de reprendre : `run`
s'arrête avec `data manifest changed since plan (dataManifestSha256)`.

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
identique renvoie `unchanged` ; un plan différent sous la même racine échoue.
Champs JSON : `status` (`created`, `unchanged` ou `failed`), `path`,
`designPositions`, `uniqueRuns`, `contextHash`, `error`. Dans un résultat
`failed`, `path` et `contextHash` valent `null` et les deux nombres valent 0.

## `check`

```text
eqasim-sa check --meta-config PATH \
  [--calibration-meta-config PATH ...] [--smoke]
```

`--calibration-meta-config` est répétable et s'ajoute aux chemins du bloc YAML
`calibration` ; les doublons sont supprimés en conservant l'ordre.

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
l'estimation `path_length` couvre donc aussi les runs du smoke. Il exige
`sampling_rate` et deux seeds explicites distinctes (sinon : `smoke requires
sampling_rate and two distinct explicit seeds`). Sa configuration de départ est
celle du premier run du plan, avec `execution.machine_options` appliqué comme
pour `run` : `execution.machine_options.data_path` est donc le dossier de
données que lisent les runs du smoke. Les indicateurs qui lisent une
[sortie d'analyse](analysis-scripts.md) sont ignorés, car `check` n'exécute
jamais les scripts d'analyse.

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
avant tout run terminé (`diskSpace` échoue alors avec `no completed calibration
run yet: run ... first`, voir [Dépannage](troubleshooting.md#no-completed-calibration-run-yet-run--first)). Avant `plan`, `sealedPlan` et `diskSpace`
échouent avec `campaign <campagne> has no plan yet: run plan first` ;
`diskSpace` donne le même message, en nommant la campagne de calibration,
quand une méta-configuration de calibration n'a pas été planifiée. `estimate`
s'arrête avec le même message.

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

Les deux valent `null` seulement dans un résultat `failed`. Un résultat `failed`
ne calcule rien : tous les champs autres que `status`, `estimateVersion` et
`error` valent `null` (jamais `0`).

## `run`

```text
eqasim-sa run --meta-config PATH
```

Exige `execution` et un plan scellé. Exécute seulement les `runId` sans marqueur
valide. Un second `run` lancé sur la même campagne pendant que le premier est
actif n'échoue pas : il attend les slots de worker et les runs que tient le
premier, et n'exécute jamais deux fois un `runId` (un run terminé entre-temps
compte comme `skipped`). Champs JSON : `status` (`completed`, ou `failed` dès
qu'un run a échoué), `total`, `completed`, `skipped`, `failed`, `failures` (un
`{runId, error}` par run en échec), `error`. Quand tout est déjà complet,
`total` vaut 0. Le code de sortie vaut 1 dès que `status` vaut `failed`.

## `analyze`

```text
eqasim-sa analyze --meta-config PATH [--descriptive-only]
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
`rankReversalsPath`, `noiseFloorPath` et `replicationAllocationPath` sont
propres à `canonical_morris` (le dernier à un pilote ; il vaut `null` en
production) ; `byScramblePath` et `diagnosticsPath` sont propres à
`multiscramble_sobol` ; `convergencePath` sert aux deux. Un chemin sans objet
vaut `null`.

`inferenceStatus` vaut `incomplete_noninferential` quand des runs manquent ou
ont échoué, `demonstration_noninferential` pour Grid, OAT discret et Sobol,
`pilot_only` pour un pilote Morris canonique, et `conditional` ou `unstable`
(critères de convergence atteints ou non) pour une production Morris canonique
et pour Sobol multi-scramble.

`--descriptive-only` ignore `analysis.completeness_threshold` et ne publie que
les sorties descriptives des runs terminés, même pour une campagne incomplète ou
vide : `indicators.parquet`, `output-summary.parquet`, `governed-report.json`
et `replication-dispersion.parquet` quand les seeds sont des réplications.
Aucune table de sensibilité n'est publiée (`sensitivityMethod` vaut `null`), et
`inferenceStatus` vaut `descriptive_noninferential`, ou
`incomplete_noninferential` quand des runs manquent.

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

## Codes de sortie

| Code | Sens |
| ---: | --- |
| 0 | succès, aide sans commande, configuration affichée, plan inchangé ou run déjà complet |
| 1 | validation invalide (y compris un fichier invalide pour `show-config`), échec métier, au moins un run échoué ou un Python autre que 3.13 |
| 2 | arguments CLI invalides produits par `argparse` |
| 130 | interruption clavier gérée au niveau de l'application |

Les erreurs applicatives sont normalement encodées dans le JSON ; `show-config`
et `plot` les écrivent en texte brut sur la sortie d'erreur. `init` et
`check` masquent les chemins absolus dans leurs messages ; cette garantie n'est
pas appliquée uniformément aux autres commandes.
