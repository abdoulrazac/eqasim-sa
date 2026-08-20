# Référence CLI

[English](../docs/cli-reference.md) | [Français](cli-reference.md)

Lorsqu'une sous-commande publique atteint son traitement applicatif, elle écrit
normalement un objet JSON sur la sortie standard. Les avertissements, notamment
la capacité Sobol, vont sur la sortie d'erreur. `--help` et l'absence de
sous-commande affichent du texte, pas du JSON. Une erreur de syntaxe `argparse`
affiche l'usage et renvoie 2. Une interruption peut terminer à 130 sans garantir
qu'un objet JSON complet ait été écrit.

## `init`

```text
eqasim-sa init --project-name NAME [options]
```

| Option | Requise | Défaut | Contrat |
| --- | --- | --- | --- |
| `--project-name NAME` | oui | aucun | segment sûr de 1 à 64 caractères |
| `--directory PATH` | non | `./NAME` | destination du projet |
| `--model-root PATH` | non | `./eqasim-france` | racine du modèle |
| `--model-config FILE` | non | `config_local_nantes.yml` | chemin relatif à `model-root`, sans `..` |
| `--data-path PATH` | non | `./eqasim-data` | arbre de données à hacher |
| `--scratch PATH` | non | `<directory>/scratch` | scratch local, créé si absent |
| `--invocation ARG` | non, répétable | `uv run --locked python -m synpp` | un argument par occurrence, dans l'ordre |
| `--regenerate-data-manifest` | non | faux | rehache et remplace uniquement `manifest.json` |

Statuts JSON : `initialized`, `unchanged` ou `failed`. Champs : `initVersion`,
`projectName`, `manifest`, `readme`, `metaConfigs`, `elapsedSeconds`, `error`.
La commande est template-only : le hachage des données n'est pas une exécution
du pipeline. Avec `--regenerate-data-manifest`, un projet existant doit toujours
passer les contrôles stricts de ses fichiers statiques et de son état durable;
seul le manifeste est remplacé. Les plans et résultats ne sont pas réécrits.

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

Sans `--smoke`, les contrôles sont :

| Contrôle | Vérification |
| --- | --- |
| `metaConfig` | YAML, contexte Git, facteurs et chemins |
| `sealedPlan` | égalité du plan avec le design recalculé |
| `uniqueConfigSeedPairs` | absence de doublons Grid; validation scellée pour Morris historique, OAT discret, Morris canonique et Sobol |
| `eqasimEnvironment` | sonde synpp avec un chemin de configuration inexistant |
| `diskSpace` | espace durable et scratch d'après les observations disponibles |

Le contrôle statique a un budget global de 55 secondes et la sonde
d'environnement utilise au plus 20 secondes de ce budget.

`--smoke` n'est pas statique. Si les contrôles précédents passent, il exécute
deux runs eqasim temporaires réels avec les deux premières seeds distinctes et
`sampling_rate: 0.001`, valide leurs marqueurs et leurs Parquet, puis supprime le
répertoire temporaire. Il exige `sampling_rate` et deux seeds explicites. Il
n'applique pas `execution.overrides` : sa configuration de départ est le YAML
modèle scellé. Ainsi, `execution.overrides.data_path` est ignoré par le smoke et
le `data_path` du modèle doit être utilisable depuis la machine qui lance le test.

Champs JSON : `status` (`valid` ou `invalid`), `checkVersion`,
`elapsedSeconds`, `checks`, les mesures `estimated/required/free/missing` pour
le disque durable et le scratch, puis `error`.

## `estimate`

```text
eqasim-sa estimate --meta-config PATH \
  [--calibration-meta-config PATH ...]
```

Exige un plan cible, un bloc `execution` et au moins un run complet parmi la
cible ou les campagnes de calibration. Pour chaque observation, `duration` et
`scratchBytes` viennent du marqueur, tandis que les octets durables sont mesurés
sur le marqueur et tous les fichiers Parquet de l'archive. Le calcul utilise la
moyenne des durées et tailles durables, et le maximum du cache par worker.

Champs JSON : `status`, `estimateVersion`, `method`, `formula`,
`formulaInputs`, `designPositions`, `runs`, `calibrationRuns`,
`meanDurationSeconds`, `meanDurableBytesPerRun`, `estimatedComputeHours`,
`estimatedDiskBytes`, `workers`, `observedScratchBytesPerWorker`,
`estimatedScratchBytes`, `error`.

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

Recalcule les indicateurs depuis les archives et publie atomiquement les sorties
d'analyse. Champs JSON : `status`, `path`, `runs`, `indicators`, `values`,
`completionRate`, `expected`, `completed`, `missing`, `failed`,
`dispersionPath`, `sensitivityMethod`, `sensitivityPath`, `convergencePath`,
`rankReversalsPath`, `error`. Les deux derniers chemins ne sont non nuls que pour
`canonical_morris`. L'ancien `morris` reste accepté comme provenance historique ;
les nouvelles démonstrations générées utilisent `discrete_oat`.

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
  --current-context-hash HASH
```

`--invocation-json` doit être un tableau JSON de chaînes. Réponse : `runId`,
`status`, `error`.

## Codes de sortie

| Code | Sens |
| ---: | --- |
| 0 | succès, aide sans commande, plan inchangé ou run déjà complet |
| 1 | validation invalide, échec métier ou au moins un run échoué |
| 2 | arguments CLI invalides produits par `argparse` |
| 130 | interruption clavier gérée au niveau de l'application |

Les erreurs applicatives sont normalement encodées dans le JSON. `init` et
`check` masquent les chemins absolus dans leurs messages; cette garantie n'est
pas appliquée uniformément aux autres commandes.
