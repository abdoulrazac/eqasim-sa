# Workflow et résultats

[English](../docs/workflow-and-results.md) | [Français](workflow-and-results.md)

## Cycle de vie

```mermaid
flowchart LR
    I[init: modèles + manifeste] --> PC[plan calibration]
    PC --> RC[run calibration]
    RC --> PT[plan cible]
    PT --> C[check statique]
    C --> E[estimate]
    E --> R[run manquants]
    R --> A[analyze]
    R -->|reprise| R
    C -. option explicite .-> S[smoke: 2 runs temporaires]
```

`init` prépare les modèles de configuration et hache les données; il n'entre pas
dans les étapes d'exécution du pipeline. La calibration fournit ensuite les
observations de durée, stockage durable et cache nécessaires au devis d'une
cible.

## Identités scellées

`contextHash` est défini par
`SHA-256(b"eqasim-sa:context:v1\0" + UTF8(descriptor))`, où `descriptor` est le
JSON compact à clés triées de `eqasimCommit`, `dataManifestSha256`,
`lockfileSha256` et `eqasimSaVersion`. Le résultat est une chaîne de 64
caractères hexadécimaux minuscules. Voir la
[définition complète](yaml-reference.md#model-et-context).

`runId` est constitué des 32 premiers hexadécimaux d'un SHA-256 sur la
configuration canonique et la seed. La canonicalisation retire
`working_directory`, `data_path`, binaires, mémoire Java, `output_path`,
`processes` et `random_seed` de la configuration avant hachage, puis porte la
seed séparément. Plus précisément, les binaires exclus sont `git_binary`,
`java_binary` et `maven_binary`, et la mémoire exclue est `java_memory`.
`output_prefix` n'est pas exclu et sa valeur affecte donc `runId`.

`designId` identifie une position statistique, pas une exécution. Il inclut les
indices de niveau et de réplication pour Grid, ou le rôle scellé du design pour
Morris et Sobol. Deux `designId` distincts peuvent donc partager un `runId`.

## Positions et runs uniques

`plan.parquet` contient une ligne par position. Ses colonnes fixes sont :

| Colonne | Type | Sens |
| --- | --- | --- |
| `designId` | string | identité de la position |
| `method` | string | `grid`, `morris` ou `sobol` |
| `role` | string | `point`, étape de trajectoire ou matrice Sobol |
| `runId` | string | identité de l'exécution dédupliquée |
| `seed` | int64 | seed eqasim |
| `contextHash` | string | contexte scientifique |
| `<factor>` | type du domaine | valeur projetée de chaque facteur hors seed |

Le schéma, ses métadonnées et toutes les lignes sont recalculés lors du
chargement. Pour Sobol, l'ordre physique des lignes peut varier, mais le contenu
indexé par `designId` doit être identique.

## Scellement du plan

`plan` prend un verrou au niveau du parent de campagne et écrit atomiquement en
Zstandard. Un plan identique reste inchangé. Si `plan.parquet` existe mais
diffère, ou si la racine contient déjà un état durable sans plan, la commande
refuse de continuer. Créez une nouvelle racine au lieu d'éditer l'existante.

## Calibration et estimation

Chaque marqueur complet stocke `duration` et `scratchBytes`. Pour chaque run
observé, l'estimateur mesure séparément les octets durables en additionnant la
taille du marqueur et de tous les fichiers Parquet plats de son archive.
`estimate` calcule :

- durée moyenne observée × nombre de runs uniques pour les heures de calcul;
- taille moyenne durable × runs uniques, plus la taille du plan, pour le disque;
- maximum `scratchBytes` observé × nombre de workers pour le scratch.

Ce sont des extrapolations moyennes, pas des garanties de quota ou de durée.
Les observations de la cible déjà complète sont également prises en compte.

## Arborescences de stockage

```text
campaigns/grid/
├── plan.parquet
├── runs/
│   └── <runId>.parquet
├── archives/
│   └── <runId>/
│       ├── persons.parquet
│       ├── households.parquet
│       ├── activities.parquet
│       ├── trips.parquet
│       ├── vehicle_types.parquet
│       ├── vehicles.parquet
│       ├── age.parquet
│       ├── age_purpose.parquet
│       ├── distance.parquet
│       ├── license.parquet
│       ├── nbr_vehicle.parquet
│       └── travel.parquet
├── logs/
│   └── <runId>.log
├── indicators.parquet
├── replication-dispersion.parquet
└── analysis-generation.json
```

Les logs n'existent que pour les échecs publiés. L'arbre montre les six artefacts
de `synthesis.output` et les six artefacts supplémentaires lorsque
`analysis.synthesis.population` est demandée. Seules les étapes requises par les
indicateurs sont exécutées, avec `synthesis.output` toujours présente.

```text
scratch/eqasim-sa/<campaign>/workers/<slot>/
├── lease.lock
├── state.json
├── cache/
└── attempt-<uuid>/
```

Le scratch contient les caches synpp et tentatives temporaires. Il est jetable.

## Marqueur de complétude

`runs/<runId>.parquet` est écrit en dernier. Il contient une seule ligne,
compressée Zstandard :

| Colonne | Sens |
| --- | --- |
| `runId`, `campaign`, `seed`, `contextHash` | identité attendue |
| `status` | toujours `completed` |
| `attempts` | toujours `1` dans le format actuel |
| `node` | nom du nœud d'exécution |
| `duration` | secondes monotoniques, float fini non négatif |
| `scratchBytes` | taille du cache worker après le run |
| facteurs | valeurs scientifiques du run |

Une archive sans marqueur est incomplète. Un marqueur au mauvais schéma, avec
une identité différente, un nom étranger ou une compression différente est une
erreur. La validation exige un fichier ordinaire sans lien symbolique, exactement
une ligne, le schéma attendu, aucune valeur nulle, les valeurs et types exacts de
`runId`, `campaign`, `seed`, `status: completed`, `attempts: 1`, `contextHash` et
des facteurs, un `duration` de type float fini et non négatif, un `scratchBytes`
entier non négatif, et la compression Zstandard de toutes les colonnes. Le
répertoire `runs/` ne peut contenir aucun nom étranger. Le champ `node` est une
chaîne non nulle validée par le schéma. L'archive est validée séparément par
`estimate` et `analyze`; le marqueur reste la preuve de complétude.

## Sorties d'analyse

### `indicators.parquet`

| Colonne | Description |
| --- | --- |
| `runId` | run source |
| `indicator` | nom résolu de l'indicateur |
| `value` | float fini calculé |

### `replication-dispersion.parquet`

Produit quand la seed est une réplication. Les colonnes de facteurs sont suivies
de `indicator`, `n`, `mean`, `std`, `cv`, `ci95_low`, `ci95_high`. L'intervalle
utilise `mean ± 1.96 × std/√n`; avec une seule observation, les statistiques de
dispersion `std`, `cv`, `ci95_low` et `ci95_high` sont nulles; `n` vaut 1 et
`mean` conserve la valeur observée.

### `morris-ranking.parquet`

Colonnes : `indicator`, `factor`, `rank`, `trajectories`, `replications`, `n`,
`mu`, `mu_star`, `sigma`, `seed_std`. Le rang est ordonné par `mu_star`
décroissant puis nom du facteur.

### `sobol-indices.parquet`

Colonnes : `indicator`, `factor`, `rank`, `base_sample_size`,
`bootstrap_resamples`, `bootstrap_attempts`, `first_order`,
`first_order_ci_low`, `first_order_ci_high`, `total_order`,
`total_order_ci_low`, `total_order_ci_high`. Le rang suit l'ordre total
décroissant; les égalités sont départagées par le nom de facteur croissant.

### `analysis-generation.json`

Manifeste version 2 avec les SHA-256 des sorties, la méthode de sensibilité et
les comptes `expected`, `completed`, `missing`, `failed` ainsi que
`completionRate`.

## Idempotence et récupération

`run` valide le plan, déduit les runs uniques, valide tous les marqueurs présents
et n'envoie que les runs manquants. Une relance après interruption reprend donc
sans recalculer les runs complets. Une relance totalement complète renvoie un
succès avec `total: 0`.

Les écritures Parquet utilisent un fichier temporaire, `fsync` et remplacement
atomique. `analyze` sauvegarde les sources et sorties avant publication et les
restaure si un indicateur les mute ou si l'analyse échoue.

Ne réparez jamais un état en créant un marqueur ou en modifiant un plan. Corrigez
la cause, supprimez seulement le scratch jetable si aucun processus n'est actif,
puis relancez la même commande.

## Workers, caches et leases

Les runs sont distribués en lots entre au plus `workers` slots. Chaque slot
traite son lot en série et réutilise son cache. Un lease privé empêche deux
processus d'utiliser le même cache. `state.json` vaut `active` ou `clean`; un
état absent, invalide ou non propre entraîne la purge du cache avant réutilisation.

Un lease par `runId` complète cette protection côté SLURM. Les caches ne sont
jamais partagés entre slots.

## Local et SLURM

Le backend local lance les lots en parallèle dans le processus de commande et
interrompt les groupes de sous-processus sur `Ctrl-C`. Le backend SLURM utilise
Submitit, un job array et `slurm_array_parallelism=workers`; chaque job recharge
la méta-configuration et revérifie son SHA-256 et le `contextHash`.

Le code et les tests couvrent la construction et le comportement simulé du
backend SLURM. Ils ne constituent pas une validation sur un cluster SLURM réel.

Voir le [glossaire](glossary.md) pour les termes d'identité et de stockage.
