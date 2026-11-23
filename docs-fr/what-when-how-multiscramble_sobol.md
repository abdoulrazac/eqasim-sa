# Sobol multi-scramble : prioriser ou tester une fixation

**Cas pratique : choisir quel réglage du matching apprendre plus précisément en premier.**

[English](../docs/what-when-how-multiscramble_sobol.md) | **Français**

## En bref

Après un criblage Morris, supposons que deux paramètres restent incertains :
`matching_minimum_observations` et `matching_minimum_age`. Vous ne voulez plus
seulement savoir s'ils ont un effet. Vous voulez décider où investir le prochain
effort de collecte ou de modélisation.

La **priorisation des facteurs** (FP) demande :

> Quel facteur devrions-nous apprendre plus précisément en premier pour réduire l'incertitude attendue de la sortie ?

La **fixation des facteurs** (FF) pose une autre question :

> L'effet total d'un facteur, interactions comprises, reste-t-il sous une tolérance pratique déclarée ?

Ces deux objectifs utilisent Sobol multi-scramble, mais ils exigent des chartes,
des budgets et des campagnes séparés. Ce guide déroule d'abord le cas FP, puis
montre la variante FF.

## Les notions indispensables

- L'**indice de premier ordre** est la part de variation associée au facteur seul.
- L'**indice d'ordre total** inclut le facteur et toutes ses interactions.
- Un **scramble** est une randomisation indépendante de la séquence Sobol.
- Un **point de contrôle** (*checkpoint*) est une taille d'échantillon où l'on vérifie la stabilité.
- Un facteur **actionnable** peut faire l'objet d'une décision ; la seed du simulateur ne l'est pas.

Plusieurs scrambles indépendants montrent comment les estimations changent d'une
randomisation Sobol à l'autre. Rééchantillonner les lignes d'un seul scramble ne
fournit pas cette preuve ; c'est pourquoi le [Sobol de démonstration](what-when-how-sobol.md)
reste non inférentiel.

## Prérequis : un criblage terminé

Le protocole Nantes n'autorise FP ou FF qu'après une production Morris canonique :

- complète ;
- convergée selon les critères déclarés ;
- portant le statut `conditional` ;
- issue d'un plan et d'un rapport gouverné scellés.

Le script de rendu vérifie cette condition d'entrée. Ne remplacez pas la campagne de criblage par un
chemin fictif et ne retirez pas un facteur simplement parce que son rang Morris
semblait faible : le criblage ne prouve jamais qu'un facteur peut être fixé.

La suite suppose que vous avez terminé le
[cas Morris canonique](what-when-how-canonical_morris.md), que `$ROOT` et le
tableau Bash `SA` sont définis, et que `mon-analyse-sa/` contient le manifeste,
le scratch et une calibration de ressources complète.

## Cas FP de bout en bout

Le cas complet se trouve dans
[`03-multiscramble-fp.yml`](../protocols/nantes-scientific/03-multiscramble-fp.yml).
Il s'agit d'un protocole concret, pas d'un fichier directement exécutable : le
script de rendu doit d'abord remplacer ses marqueurs machine et y inscrire la provenance
du criblage. Ce guide n'affirme pas qu'une campagne Nantes réelle a déjà produit
les résultats fictifs présentés plus bas.

### 1. Poser la décision

La charte demande lequel des deux réglages numériques doit être appris plus
précisément en premier pour réduire la variation de l'indicateur âge :

```yaml
decision:
  claim: Prioritize which of the two declared Nantes numerical factors should be learned more accurately first
  use: diagnostic
  setting: Loire-Atlantique department 44 population synthesis using INSEE RP 2022 and EDGT 44
  objective: fp
  estimand: First-order Sobol variance shares for age total-variation distance over the two declared numerical factors and finite seed support
  primary_indicator: tv_distance(age, ref=INSEE)
```

Une décision FP classe les facteurs actionnables par leur indice moyen de premier
ordre. Elle ne dit pas encore qu'un facteur peut être fixé.

### 2. Déclarer les facteurs, y compris le bruit du simulateur

```yaml
parameters:
  matching_minimum_observations:
    values: [5, 20, 50]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Production screening report sha256:<empreinte réelle du criblage>
  matching_minimum_age:
    values: [0, 5, 12]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Production screening report sha256:<empreinte réelle du criblage>
```

La seed du simulateur est le troisième facteur, nommé `seed`. Elle ne se déclare
pas sous `parameters` mais avec l'expérience :

```yaml
experiment:
  seeds: [44011, 44012, 44013, 44014]
  seeds_provenance: Declared finite simulator-seed nuisance support; screening sha256:<empreinte réelle du criblage>
```

Sa distribution est implicitement `discrete_uniform` sur ces quatre seeds, et
`seeds_provenance` tient le rôle de sa `provenance`.

Les deux réglages de matching sont actionnables. `seed` représente une source de
variation aléatoire nécessaire au calcul, mais personne ne va « apprendre la
seed plus précisément ». Elle reste donc diagnostique et ne reçoit aucun rang FP
actionnable.

Chaque facteur déclaré sous `parameters` doit déjà exister sous `config:` dans
votre configuration eqasim de Nantes ; le facteur seed n'a besoin d'aucune clé du modèle. Ne
déclarez jamais `random_seed` ni `seed` sous `parameters`.

Le support et l'égalité des probabilités doivent être justifiés par l'étude. Une
liste de valeurs pratique n'est pas, à elle seule, une preuve scientifique.

### 3. Déclarer l'indicateur principal

Comme dans Morris canonique, le protocole calcule les TV âge contre INSEE,
distance contre HTS et nombre de véhicules contre HTS. Seule
`tv_distance(age, ref=INSEE)` gouverne la décision FP. Les autres indicateurs
servent à repérer des effets secondaires, pas à changer silencieusement la
question après avoir vu les résultats.

Pour Nantes/EDGT 44, l'indicateur de distance est asymétrique : les classes
synthétiques utilisent la distance euclidienne entre activités, tandis que HTS
utilise la distance routée. Il reste un diagnostic secondaire.

### 4. Déclarer les scrambles et les points de contrôle

```yaml
experiment:
  method: multiscramble_sobol
  seeds: [44011, 44012, 44013, 44014]
  seeds_provenance: Declared finite simulator-seed nuisance support; screening sha256:<empreinte réelle du criblage>
  base_sample_sizes: [8, 16, 32]
  scramble_seeds: [44101, 44102, 44103, 44104]
analysis:
  completeness_threshold: 1.0
  confidence_level: 0.95
```

Les quatre seeds de scramble créent quatre randomisations indépendantes. Dans
chaque scramble, les tailles `8`, `16` et `32` sont emboîtées : le point de contrôle 16
réutilise les 8 premiers points et le point de contrôle 32 réutilise les 16 premiers.

Le nombre maximal de positions utilise `S = 4` scrambles, `N = 32` et `D = 3`
facteurs, seed comprise :

```text
S × N × (2D+2) = 4 × 32 × 8 = 1 024 positions
```

Le champ de budget vaut 1 280 avec 20 % de réserve, soit exactement **1 024
positions structurelles** disponibles pour le design. Après projection sur les
supports discrets `3 × 3 × 4`, ces positions représentent **36 `runId` uniques**.
Le budget scientifique est volontairement contrôlé sur les positions, tandis que
`run` n'exécute chaque `runId` qu'une fois. Cette égalité est un choix explicite
du protocole, pas une règle générale.

### 5. Rendre la campagne FP

Depuis la racine `openmole-eqasim`, après la production Morris canonique :

```bash
uv run --project "$ROOT/eqasim-sa" --locked python \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/render_protocol.py" \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/03-multiscramble-fp.yml" \
  "$ROOT/rendered/multiscramble-fp.yml" \
  --model-config "$ROOT/eqasim-france/config_nantes_sa.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/mon-analyse-sa/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/mon-analyse-sa/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/mon-analyse-sa/campaigns/multiscramble-fp" \
  --screening-campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-production"
```

Le script de rendu refuse de produire le YAML si les preuves de criblage sont absentes,
incomplètes, instables ou incompatibles. `config_nantes_sa.yml` est la même copie
de la configuration eqasim de Nantes que pour Morris canonique. Le script de rendu
vérifie aussi que la campagne de criblage a été planifiée avec la même
empreinte du code d'eqasim, le même manifeste de données, le même lockfile et la
même version d'`eqasim-sa`.

### 6. Planifier, chiffrer et exécuter

```bash
TARGET="$ROOT/rendered/multiscramble-fp.yml"
CALIBRATION="$ROOT/mon-analyse-sa/configs/00-calibration.yml"

"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" estimate --meta-config "$TARGET" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

La calibration indiquée doit déjà contenir au moins un run complet et être
compatible avec les ressources de cette exécution. Sans observation compatible,
suivez la procédure de votre site avant d'exécuter les 36 runs uniques représentés
par les 1 024 positions.

### 7. Lire une décision FP

Les fichiers d'analyse se trouvent dans la génération d'analyse courante de la
campagne, `analysis/generations/<generationId>/` (désignée par
`analysis-generation.json`). Le fichier `multiscramble-sobol-indices.parquet` agrège les quatre scrambles au
point de contrôle final. Pour FP, regardez les colonnes de premier ordre, le rang et
`is_actionable`.

Illustration **fictive** :

| Facteur | Premier ordre moyen | Intervalle entre scrambles | Actionnable | Rang FP |
| --- | ---: | ---: | --- | ---: |
| `matching_minimum_observations` | `0.41` | `0.34-0.48` | oui | `1` |
| `matching_minimum_age` | `0.16` | `0.09-0.23` | oui | `2` |
| `seed` | `0.27` | `0.18-0.36` | non | vide |

Dans cet exemple fictif, la priorité d'apprentissage conditionnelle est
`matching_minimum_observations`, car son indice moyen de premier ordre est le
plus grand parmi les facteurs actionnables. La seed peut expliquer une variation
visible, mais elle ne devient pas une cible d'apprentissage.

Avant d'écrire cette phrase, vérifiez :

- `multiscramble-sobol-by-scramble.parquet` pour voir les quatre estimations indépendantes ;
- `multiscramble-sobol-convergence.parquet` pour la stabilité entre points de contrôle ;
- `multiscramble-sobol-diagnostics.parquet` pour les valeurs hors plage, contrastes et duplications ;
- `convergence-evidence.parquet` pour le passage du critère `ci_width` ;
- `governed-report.json` pour le statut de la campagne.

Les indices bruts ne sont pas tronqués dans `[0, 1]`. Une valeur hors plage avec
un échantillon fini reste visible dans les diagnostics et invite à augmenter le
budget ou à revoir le design.

Un statut `unstable` interdit la priorité. Un statut `conditional` autorise
seulement une priorité conditionnelle au cadre déclaré. Le contrat actuel laisse
les contrôles de robustesse en attente et porte `claimAuthorized: false` ; il ne
produit donc pas une revendication finale robuste.

En option, dessiner les figures de l'analyse publiée :

```bash
"${SA[@]}" plot --meta-config "$TARGET"
```

Valides pour `multiscramble_sobol` : `output-summary` (une ligne par indicateur : médiane, q05 à q95 et min à max sur les positions du plan, jamais une incertitude du modèle) ; `multiscramble-indices` (moyennes des ordres 1 et total avec leur IC au dernier point de contrôle, avec le rang de chaque facteur actionnable selon la règle de classement de l'objectif) ; `multiscramble-convergence` (largeur d'IC selon la taille de base N ; exige la table de convergence et le rapport gouverné) ; et `multiscramble-by-scramble` (un point par scramble et par facteur au dernier point de contrôle). Chacune porte la ligne de pied avec `claimAuthorized` (aujourd'hui toujours `false`) : aucune n'autorise une conclusion finale. Voir [Figures](workflow-and-results.md#figures).

## Variante FF : tester si un facteur peut être fixé

FF doit être une autre campagne, avec l'objectif `ff` et une tolérance déclarée
**avant** `plan`. Le protocole Nantes
[`04-multiscramble-ff.yml`](../protocols/nantes-scientific/04-multiscramble-ff.yml)
utilise `practical_tolerance: 0.05`, des points de contrôle `[16, 32, 64]` et quatre
autres seeds de scramble.

Le script de rendu s'appelle de la même manière en remplaçant la source, la sortie et
la racine :

```bash
uv run --project "$ROOT/eqasim-sa" --locked python \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/render_protocol.py" \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/04-multiscramble-ff.yml" \
  "$ROOT/rendered/multiscramble-ff.yml" \
  --model-config "$ROOT/eqasim-france/config_nantes_sa.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/mon-analyse-sa/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/mon-analyse-sa/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/mon-analyse-sa/campaigns/multiscramble-ff" \
  --screening-campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-production"
```

Exécutez ensuite toute la campagne FF, séparément de FP :

```bash
FF_TARGET="$ROOT/rendered/multiscramble-ff.yml"
CALIBRATION="$ROOT/mon-analyse-sa/configs/00-calibration.yml"

"${SA[@]}" plan --meta-config "$FF_TARGET"
"${SA[@]}" check --meta-config "$FF_TARGET" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" estimate --meta-config "$FF_TARGET" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" run --meta-config "$FF_TARGET"
"${SA[@]}" analyze --meta-config "$FF_TARGET"
```

Le plan FF contient `4 × 64 × 8 = 2 048` positions structurelles, projetées sur
les mêmes **36 runs uniques**. Lisez ensuite `multiscramble-sobol-indices.parquet`
dans la génération d'analyse courante de
`$ROOT/mon-analyse-sa/campaigns/multiscramble-ff/` (le dossier
`analysis/generations/<generationId>/` désigné par `analysis-generation.json`).

Pour l'indicateur principal, FF compare la **borne supérieure de l'intervalle
d'ordre total**, colonne `total_order_ci_high`, à `0.05`. Cette tolérance est une
part de variance totale sans unité ; ce n'est pas `0.05` unité de distance TV.
La colonne `within_practical_tolerance` enregistre la comparaison pour les
facteurs actionnables de l'indicateur principal. Si la borne vaut fictivement
`0.04`, le facteur est conditionnellement dans la tolérance, interactions
incluses. Si elle vaut `0.08`, il ne l'est pas. Une estimation centrale inférieure
à `0.05` ne suffit pas si sa borne supérieure dépasse la tolérance.

N'utilisez cette classification que si `convergence-evidence.parquet` confirme
le passage du critère `ci_width` et si `governed-report.json` porte le statut
`conditional`. Avec `unstable`, la colonne calculée reste diagnostique et ne
permet aucune décision FF.

La seed et les indicateurs secondaires ne reçoivent jamais de décision FF.

## Ce que vous pouvez conclure

Pour FP, après convergence, vous pouvez nommer une priorité d'apprentissage
conditionnelle parmi les facteurs actionnables. Pour FF, **après convergence**,
vous pouvez dire qu'un facteur est conditionnellement dans la tolérance déclarée
pour l'indicateur principal, interactions incluses.

Vous ne pouvez pas étendre cette conclusion à d'autres supports, dépendances,
indicateurs ou scénarios. Vous ne pouvez ni classer ni fixer la seed. Avec
`claimAuthorized: false`, la phrase reste conditionnelle et non autorisée comme
revendication robuste finale.

## Points à vérifier

- Une production Morris canonique complète, convergée et conditionnelle existe.
- La charte choisit exactement un objectif, FP ou FF.
- Les supports, probabilités et indépendances sont justifiés.
- Au moins quatre scrambles indépendants et des tailles qui doublent sont déclarés.
- `seed` est présente comme facteur fini non actionnable.
- Pour FF, la tolérance est déclarée avant `plan` et l'ordre total est utilisé.
- La convergence, les diagnostics et le rapport gouverné sont lus avant toute conclusion.

## Suite

- Étape précédente : [Morris canonique](what-when-how-canonical_morris.md)
- Démonstration seulement : [Sobol de démonstration](what-when-how-sobol.md)
- Protocole complet : [Nantes scientifique](../protocols/nantes-scientific/README.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [déroulement et résultats](workflow-and-results.md)
