# Compatibilité de version

[English](../docs/compatibility.md) | **Français**

Cette page décrit `eqasim-sa` 0.3.0 ainsi que les changements listés sous
Unreleased dans le [journal des modifications](../CHANGELOG.md), parmi lesquels
les facteurs structurés, le modèle eqasim identifié sans Git, les scripts
d'analyse, `show-config` et `plot`. L'analyseur syntaxique Python et le code
d'analyse font foi. Il n'y a aucune rétrocompatibilité : seuls les formats
courants sont lus et rien n'est migré.

## Matrice des plans et méthodes

| Méthode | Charte acceptée | Objectif et contrat stochastique | Schéma du plan | Métadonnées de méthode | Sens dans cette version |
| --- | --- | --- | --- | --- | --- |
| `grid` | démonstration, y compris une charte omise | aucun ; la seed est une réplication | v1 ; v3 avec facteur structuré | aucune | démonstration uniquement ; premier choix recommandé pour une structure nommée |
| `discrete_oat` | démonstration uniquement | effets locaux normalisés entre niveaux adjacents ; la seed est une réplication | v1 | `discreteOatDesignVersion` v1 | criblage de démonstration ; pas Morris canonique |
| `sobol` | démonstration, y compris une charte omise | un scramble et bootstrap de lignes ; la seed est un facteur | v1 ; v3 avec facteur structuré | `sobolDesignVersion` v1 | démonstration non inférentielle uniquement |
| `canonical_morris` | scientifique uniquement | `screening` ; `conditional_mean` ; CRN dans la trajectoire ; allocation pilote ou production déclarée | v2 | `canonicalMorrisDesignVersion` v3 (SALib) | le v3 courant permet le criblage scientifique soumis au budget, à l'arrêt et à la robustesse |
| `multiscramble_sobol` | scientifique uniquement | `fp` ou `ff` ; `finite_seed_factor` ; dépendance indépendante ; au moins quatre scrambles indépendants explicites | v2 ; v3 avec facteur structuré | `multiScrambleSobolDesignVersion` v3 (SALib) | le v3 courant permet la FP/FF scientifique soumise à une distribution de labels défendable, au budget, à l'arrêt et à la robustesse |

Tous les plans utilisent `canonicalizationVersion` v1 et portent la
métadonnée scellée `modelDocument` (la configuration eqasim canonique sans les
clés propres à la machine ni `random_seed`) ; un plan qui ne la porte pas est
refusé, Grid compris. Le schéma v3 est écrit
uniquement pour un plan structuré et ajoute la paire canonique exacte
`structuredFactorsVersion: 1` et `structuredFactors`. Sous le schéma de plan v3,
Sobol de démonstration et Sobol multi-scramble recopient aussi la correspondance
complète label-vers-liste dans leurs métadonnées de méthode comme second
invariant scellé ; les deux correspondances canoniques doivent être exactement
égales. La version
du schéma de plan est indépendante de la version du design numérique. Les designs
scientifiques sont en version v3 (contrat SALib). Les analyseurs scientifiques v3 reconstruisent strictement les
métadonnées scellées du plan, son contexte et son design SALib. Les analyseurs de
démonstration valident leurs contrats propres. L'analyse Grid directe valide la forme et les
labels v3, mais ne dispose d'aucune métadonnée de méthode ni configuration modèle
pour lier les listes. La vérification préalable de l'application (CLI) fait
autorité pour cette liaison complète : elle reconstruit tout plan structuré
depuis la méta-configuration et le
`modelDocument` scellé avant l'analyse cœur.
Il n'existe aucune migration sémantique ou en place entre les versions de plan.

## Matrice d'analyse et de revendication

Chaque analyse écrit `analysis-generation.json` version 8 (avec
`provenance.analyses` et `provenance.analysisEnvironments`), `captured-run-set.json` en version de schéma 2 (avec
`analyses` dans chaque run) et `governed-report.json` version 1.
Pour une analyse scientifique v3, `provenance.numericalEngine` est
exactement le descripteur scellé dans le plan.

| État | `inferenceStatus` | Preuve de convergence | Comportement de revendication |
| --- | --- | --- | --- |
| Grid ou OAT discret | `demonstration_noninferential` | absente | `claimAuthorized: false` |
| Sobol de démonstration à un scramble | `demonstration_noninferential` | absente | `claimAuthorized: false` |
| Pilote Morris canonique | `pilot_only` | publiée, sans promouvoir le pilote | `claimAuthorized: false` |
| Production Morris canonique | `conditional` si tous les critères d'arrêt passent, sinon `unstable` | requise et évaluée | robustesse en attente ; `claimAuthorized: false` |
| Sobol multi-scramble FP/FF | `conditional` si tous les critères d'arrêt passent, sinon `unstable` | requise et évaluée entre scrambles indépendants | robustesse en attente ; `claimAuthorized: false` |

Le criblage conserve les facteurs à examiner ; il n'estime pas des parts de
variance et ne justifie pas leur fixation. FP indique quelle incertitude apprendre
plus précisément en premier. FF exige une tolérance pratique déclarée sur la part
de variance d'ordre total, interactions incluses. La seed finie est non
actionnable et exclue des rangs de décision FP/FF.

## Matrice des refus

| État incompatible | Refus |
| --- | --- |
| `discrete_oat` ou `sobol` avec une charte scientifique | rejeté ; ces méthodes exigent `charter.status: demonstration` |
| `canonical_morris` ou `multiscramble_sobol` sans charte scientifique | rejeté ; ces méthodes exigent `charter.status: scientific` |
| Morris canonique avec un objectif autre que `screening` | rejeté |
| Morris canonique sans `conditional_mean` ou sans dépendance indépendante | rejeté |
| Sobol multi-scramble sans objectif `fp`/`ff`, `finite_seed_factor` ou dépendance indépendante | rejeté |
| Tolérance FF hors de l'intervalle sans dimension `[0,1]` | rejetée |
| `seed` structurée, alias de listes identiques, liste ordonnée mal formée ou clé modèle qui n'est pas une liste de chaînes | rejeté |
| Facteur structuré avec `discrete_oat` ou `canonical_morris` | rejeté car l'ordre nominal des labels est arbitraire |
| Métadonnées scellées d'un plan scientifique absentes, non prises en charge, non canoniques, altérées ou incompatibles avec le contexte | rejeté avant publication par les analyseurs Morris canonique et Sobol multi-scramble |
| Sortie connue laissée à son `file` par défaut dont l'étape eqasim manque dans `run:` | rejetée au chargement de la méta-configuration |
| Sortie dont le suffixe de `file` (une fois `{prefix}` remplacé) ne peut pas nommer un fichier d'archive | rejetée au chargement de la méta-configuration |
| Sortie CSV connue de la population, ou sortie `.gpkg`, quand `output_formats` de la configuration eqasim est présent sans `csv`, ou sans `gpkg` | rejetée au chargement de la méta-configuration |
| Indicateur `share`, `value` ou `tv_distance` sur une sortie dont le `file` n'est pas un fichier `.csv` | rejeté au chargement de la méta-configuration |
| Entrée d'indicateur dont la clé n'est pas `tv_distance`, `share` ou `value` | rejetée au chargement de la méta-configuration |
| `share.category` ou valeur de `where` qui n'est pas une chaîne YAML entre guillemets | rejeté au chargement de la méta-configuration |
| Plan Grid présenté à l'analyse | l'analyse cœur directe valide méthode, marqueurs, complétude, forme et labels v3 ; la vérification préalable de l'application reconstruit le plan pour lier les listes au modèle et à la méta-configuration |
| Positions scientifiques au-dessus du budget déclaré après réserve | rejeté avant publication du plan |
| Analyse OAT ou Sobol incomplète | rejetée car la complétude doit valoir `1.0` |
| Preuve d'arrêt non prise en charge ou non évaluable | échec fermé, donc non convergée |
| Plan sans la métadonnée scellée `modelDocument` | refusé par `run`, `check`, `estimate` et `analyze` : `plan.parquet has no sealed modelDocument; plan a new campaign` |
| Entrée d'analyse non déclarée dans `outputs`, autre analyse, ou fichier qui n'est ni `.csv` ni `.gpkg` | rejetée au chargement de la méta-configuration |
| Script d'analyse absent, qui n'est pas un fichier `.py` ordinaire, ou lien symbolique | rejeté au chargement de la méta-configuration |
| Analyse nommée comme une sortie (sans tenir compte de la casse) ou `logs` | rejetée au chargement de la méta-configuration |
| `analyses` avec une `execution.invocation` qui ne se termine pas par `-m synpp` et sans `execution.python` | rejeté au chargement de la méta-configuration |
| `primary_indicator` qui lit une sortie d'analyse | rejeté au chargement de la méta-configuration |
| Erreur d'un script d'analyse pour un run quelconque | toute l'analyse échoue, quel que soit `completeness_threshold` |

## Plateformes et domaines de hachage

`eqasim-sa` prend en charge nativement Windows 10+, macOS 13+ et Linux avec le
backend `local` (vérification sous Windows et macOS en attente, voir la
[vérification des plateformes](../docs/platform-verification.md)) ; `slurm` est
réservé à Linux. Les plans et leurs preuves utilisent exactement les domaines
ci-dessous :

| Domaine | Courant |
| --- | --- |
| Contexte d'exécution (`eqasimSourceSha256`) | `eqasim-sa:context:v3` |
| Empreinte du code d'eqasim | `eqasim-sa:model-source:v1` |
| Preuves d'exécution (`schemaVersion: 3`, `modelSourceSha256`) | `eqasim-sa:execution-evidence:v3` |
| Identité logicielle d'`eqasim-sa` | `eqasim-sa:software-identity:v2` |
| Définition d'indicateur (`tv_distance`, `share`, `value` ; une sortie d'analyse est décrite par `{name, analysis, inputs, separator}`) | `eqasim-sa:indicator-definition:v4` |
| Résultat d'analyse (analyse, script, entrées, paramètres du run, environnement) | `eqasim-sa:analysis-result:v1` |

Les entrées texte sont
hachées après normalisation CRLF vers LF (voir le
[dépannage](troubleshooting.md#fins-de-ligne-et-gitattributes)). Chaque worker
utilise `<scratch>/esa/<campagne>/w<slot>/`, qui contient son dossier de cache
`c<12 hex>/` et ses dossiers de tentative `a<12 hex>/`.

## Modèle eqasim et sorties

- `model.root` est n'importe quel dossier eqasim (clone, copie ou archive). Git
  n'est pas nécessaire : le code est identifié par une empreinte de son
  contenu, et `model.config` est lu relativement à `model.root` (ou comme un
  chemin absolu).
- synpp exécute la liste `run:` de la configuration eqasim sans la modifier.
- Chaque sortie déclarée est archivée à l'octet près sous le nom
  `<nom><extension>` (par exemple `age.csv`), avec son chemin d'origine dans le
  manifeste du run. Les résultats d'analyse (`indicators.parquet`, tables de
  sensibilité) sont des fichiers Parquet.
- Le plan scelle les sorties déclarées (`outputsDeclaration` : nom, `file`,
  `separator`) ; `category`, `simulated` et `references` peuvent changer entre
  les runs et l'analyse.
- Le plan scelle aussi la configuration eqasim (`modelDocument`, sans les clés
  propres à la machine) ; `analyze` vérifie la méta-configuration par rapport à
  elle, si bien qu'eqasim peut changer après les runs sans bloquer l'analyse.
- Les types d'indicateur sont `tv_distance`, `share` et `value`. Les calculs
  personnalisés sont des [scripts d'analyse](analysis-scripts.md) (`analyses`),
  exécutés par `analyze` sur les fichiers archivés avec l'interpréteur Python
  d'eqasim, ou une étape d'analyse eqasim dont le fichier CSV est lu avec
  `value`.
- `plot` dessine les figures de la génération d'analyse publiée dans
  `figures/<generationId>/` de la campagne, avec `figures.json` ; les figures ne
  font pas partie de la génération scellée.

## Frontières de init et Nantes

`eqasim-sa init` génère seulement des fichiers de départ de démonstration pour calibration,
Grid, OAT discret et Sobol de démonstration à un scramble. Il ne génère ni fichier de départ
scientifique Morris canonique ou Sobol multi-scramble, ni protocole Nantes, ni
plan, ni sortie de simulation ou d'analyse.

`protocols/nantes-scientific/` est un protocole du dépôt, pas une donnée du
paquet ni un modèle de `init`. Son script de rendu ne produit une méta-configuration de
phase qu'après vérification des plans et preuves amont scellés. Il refuse sans
créer de sortie les preuves absentes, liées symboliquement, mal formées,
altérées, incompatibles avec le contexte ou la phase. Les tests du script de rendu
emploient de faux modèles et preuves temporaires. Ils n'établissent pas que le
protocole Nantes réel a été planifié ou exécuté.

## Matrice de version

| Version | Python | Schémas de plan lus/écrits | Manifeste d'analyse | Migration |
| --- | --- | --- | --- | --- |
| 0.3.0 + Unreleased | `>=3.13,<3.14` | schémas de plan v1/v2/v3 ; design scientifique v3 uniquement | v8 | aucune |

Voir les [preuves de version](../docs/release-evidence-0.3.0.md) pour les
commandes réellement exécutées, les empreintes des artefacts et les limites de
l'environnement.
