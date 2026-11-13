# Sobol de démonstration : tester le workflow Sobol à un scramble

**Cas pratique : vérifier que la chaîne Sobol générée produit bien ses fichiers d'analyse.**

[English](../docs/what-when-how-sobol.md) | **Français**

## En bref

Le starter `sobol` fait varier plusieurs entrées, puis calcule des indices de
premier ordre et d'ordre total. Il utilise cependant une seule séquence de Sobol
randomisée, appelée **scramble**, et un bootstrap des lignes de cette même
séquence.

Ce design est une démonstration non inférentielle. Il
répond uniquement à la question :

> La planification, l'exécution et l'analyse du workflow Sobol à un scramble fonctionnent-elles ?

Il ne fournit pas la preuve d'incertitude nécessaire à une priorisation ou une
fixation scientifique. Pour cela, utilisez
[Sobol multi-scramble](what-when-how-multiscramble_sobol.md).

## Trois notions avant de commencer

- L'**indice de premier ordre** décrit la variation associée à un facteur seul.
- L'**indice d'ordre total** inclut ce facteur et ses interactions avec les autres.
- Une **interaction** signifie que l'effet d'un facteur dépend de la valeur d'un autre.

Dans cette méthode, la seed du simulateur est elle-même un facteur. Elle ne se
trouve donc pas dans `experiment.replications.seeds`.

## Cas pratique de bout en bout

Ce guide suppose que vous avez déjà généré `mon-analyse-sa/` et défini `$ROOT`
et le tableau Bash `SA` en suivant le
[cas Grid](what-when-how-grid.md#2-générer-le-projet-de-départ). Lancez les
commandes depuis ce projet généré.

### 1. Comprendre la démonstration générée

Après `eqasim-sa init`, le fichier rendu `configs/03-sobol.yml` pose une petite
question de plomberie : « Les valeurs numériques, les catégories et les seeds
finies traversent-elles correctement tout le workflow ? »

Il contient exactement :

```yaml
charter:
  status: demonstration

analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 99
  confidence_level: 0.95

experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 4
  design_seed: 20260814

parameters:
  sampling_rate:
    values: [0.001, 0.002]
  matching_age_scheme:
    values: [matching_6, analysis_7]
  seed:
    values: [1401, 1402]
```

Le fichier source `src/eqasim_sa/templates/03-sobol.yml` contient des
placeholders. Exécutez la copie rendue dans votre projet, pas ce fichier source.

Les deux paramètres ordinaires doivent déjà exister sous `config:` dans le
modèle scellé. `seed` est l'unique exception spéciale : elle ne doit pas exister
sous `config:`. N'ajoutez jamais `random_seed` comme facteur.

### 2. Comprendre ce qui est mesuré

Le starter déclare trois indicateurs TV :

| Indicateur | Comparaison |
| --- | --- |
| `tv_distance(age, ref=INSEE)` | distribution d'âge synthétique contre INSEE |
| `tv_distance(distance, ref=HTS)` | distribution de distance contre HTS |
| `tv_distance(nbr_vehicle, ref=HTS)` | distribution du nombre de véhicules contre HTS |

Pour Nantes/EDGT 44, la distance est un diagnostic secondaire asymétrique : les
classes synthétiques utilisent la distance euclidienne entre activités, tandis
que HTS utilise la distance routée.

La TV va de `0` à `1` ; une valeur plus faible indique des distributions plus
proches. Le paramètre `matching_age_scheme` touche au matching, tandis que
`analysis_age_scheme` reste fixé à `analysis_7` dans le modèle pour garder les
classes de sortie comparables.

### 3. Comprendre la taille du plan

Sobol compte `D = 3` facteurs : `sampling_rate`, `matching_age_scheme` et
`seed`. Avec `N = 4`, le plan contient :

```text
N(2D+2) = 4 × (2×3+2) = 32 positions
```

La projection sur seulement deux valeurs par facteur crée des positions qui
désignent le même réglage final. Le starter contient normalement **32 positions
statistiques mais 8 runs uniques**. Ces duplications sont attendues dans cette
petite démonstration ; elles ne rendent pas le design scientifiquement suffisant.

### 4. Calibrer, planifier et exécuter

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml

TARGET=configs/03-sobol.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

`analyze` crée directement les indicateurs, les indices et le rapport gouverné.
Il n'existe pas de commande `report` distincte.

### 5. Lire les résultats sans les surinterpréter

Les fichiers se trouvent sous `campaigns/sobol/`. Le fichier
`sobol-indices.parquet` contient, pour chaque facteur et chaque indicateur :

| Colonne | Signification dans cette démonstration |
| --- | --- |
| `first_order` | estimation à scramble unique de l'effet du facteur seul |
| `total_order` | estimation à scramble unique incluant les interactions |
| `*_ci_low`, `*_ci_high` | intervalle obtenu par bootstrap des lignes du scramble unique |
| `rank` | ordre décroissant selon l'indice total |

Voici une **illustration fictive**, et non un résultat Nantes :

| Facteur | Premier ordre | Ordre total |
| --- | ---: | ---: |
| `sampling_rate` | `0.28` | `0.42` |
| `matching_age_scheme` | `0.12` | `0.31` |
| `seed` | `0.25` | `0.37` |

Vous pouvez vérifier que le fichier est produit, que chaque facteur apparaît et
que les colonnes sont finies. Vous ne devez pas transformer ce tableau fictif en
phrase telle que « `sampling_rate` explique 28 % avec certitude ».

Les estimations brutes peuvent même sortir de `[0, 1]` avec un échantillon fini.
Cela ne doit pas être masqué ou tronqué : c'est un signal diagnostique de
l'instabilité de l'estimation.

Le problème central est l'incertitude : les intervalles proviennent du
rééchantillonnage des lignes d'un seul scramble. Ils ne montrent pas comment les
indices changeraient entre plusieurs séquences Sobol randomisées indépendamment.

Le rapport `governed-report.json` l'indique avec
`inferenceStatus: demonstration_noninferential` et `claimAuthorized: false`.

## Adapter la démonstration

Vous pouvez tester une autre entrée scalaire du modèle, par exemple
`matching_minimum_observations: {values: [5, 20, 50]}`, dans une **nouvelle**
campagne et une nouvelle racine. Le coût suit toujours `N(2D+2)`.

N'ajoutez `income_assignation_method` que si vous disposez d'un indicateur de
revenu adapté : les trois indicateurs du starter ne mesurent pas directement le
revenu. N'ajoutez `hts` que si les deux jeux de données préparés existent, car ce
paramètre change à la fois les donneurs et les références HTS.

Pour comparer simplement des catégories ou des listes structurées, préférez
[Grid](what-when-how-grid.md). Il est plus lisible et n'incite pas à interpréter
ces indices comme des preuves.

## Ce que vous pouvez conclure

Vous pouvez dire que le starter a validé la chaîne et produit les
fichiers Sobol attendus.

Vous ne pouvez pas faire une revendication scientifique de priorisation ou de
fixation, ni présenter le bootstrap d'un scramble comme l'équivalent de plusieurs
scrambles indépendants.

## Checklist

- La campagne porte `charter.status: demonstration`.
- `base_sample_size` est une puissance de deux d'au moins 2.
- `seed` est un facteur entier avec au moins deux valeurs distinctes.
- Les positions et les runs uniques sont distingués.
- Les indices hors plage éventuels restent visibles.
- Le résultat est présenté comme `demonstration_noninferential`.

## Suite

- Comparaison simple : [Grid](what-when-how-grid.md)
- Analyse scientifique : [Sobol multi-scramble](what-when-how-multiscramble_sobol.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
