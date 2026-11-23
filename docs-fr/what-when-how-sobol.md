# Sobol de démonstration : tester le workflow Sobol à un scramble

**Cas pratique : vérifier que la chaîne Sobol générée produit bien ses fichiers d'analyse.**

[English](../docs/what-when-how-sobol.md) | **Français**

## En bref

Le starter `sobol` fait varier ses entrées (le taux d'échantillonnage et la
graine du simulateur), puis calcule des indices de
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

Dans cette méthode, la seed du simulateur est elle-même un facteur, nommé
`seed`. Ses valeurs s'écrivent toujours dans `experiment.seeds`, mais elles
entrent dans la décomposition de variance au lieu d'être des réplications.

## Cas pratique de bout en bout

Ce guide suppose que vous avez déjà généré `mon-analyse-sa/` et défini `$ROOT`
et le tableau Bash `SA` en suivant le
[cas Grid](what-when-how-grid.md#2-générer-le-projet-de-départ). Lancez les
commandes depuis ce projet généré.

### 1. Comprendre la démonstration générée

Après `eqasim-sa init`, le fichier rendu `configs/03-sobol.yml` pose une petite
question de plomberie : « Les valeurs numériques et un ensemble fini de seeds
traversent-ils correctement tout le workflow ? »

Sa partie expérience se lit ainsi :

```yaml
campaign: mon-analyse-sa-sobol
experiment:
  method: sobol
  seeds: [1401, 1402]
  base_sample_size: 8
parameters:
  sampling_rate: [0.001, 0.002]
analysis:
  bootstrap_resamples: 99
```

Le reste du fichier déclare les sorties (les fichiers de population et les
trois distributions comparées aux données de référence), le fichier de
calibration, le modèle et l'exécution locale. Les clés omises prennent leur valeur par défaut :
sans `charter`, la campagne est une démonstration, l'analyse utilise
`completeness_threshold: 1.0` et `confidence_level: 0.95`, et la graine de design
est dérivée du nom de campagne. Seul `bootstrap_resamples` est abaissé à 99 pour
garder la démonstration rapide. `show-config` affiche toutes les valeurs.

Le fichier source `src/eqasim_sa/templates/03-sobol.yml` contient des
placeholders. Exécutez la copie rendue dans votre projet, pas ce fichier source.

`sampling_rate` doit exister sous `config:` dans votre configuration eqasim ; le
`config.yml` officiel le contient déjà. Comme pour tous les modèles, la liste
`run:` de cette configuration doit contenir `analysis.synthesis.population`
(voir [Démarrage](getting-started.md#préparer-votre-configuration-eqasim)). Le
facteur seed n'a besoin d'aucune clé du modèle. N'ajoutez jamais
`random_seed` ni `seed` sous `parameters`.

### 2. Comprendre ce qui est mesuré

Sans bloc `indicators`, le starter calcule trois indicateurs TV :

| Indicateur | Comparaison |
| --- | --- |
| `tv_distance(age, ref=INSEE)` | distribution d'âge synthétique contre INSEE |
| `tv_distance(distance, ref=HTS)` | distribution de distance contre HTS |
| `tv_distance(nbr_vehicle, ref=HTS)` | distribution du nombre de véhicules contre HTS |

Pour Nantes/EDGT 44, la distance est un diagnostic secondaire asymétrique : les
classes synthétiques utilisent la distance euclidienne entre activités, tandis
que HTS utilise la distance routée.

La TV va de `0` à `1` ; une valeur plus faible indique des distributions plus
proches.

### 3. Comprendre la taille du plan

Sobol compte `D = 2` facteurs : `sampling_rate` et `seed`. Avec `N = 8`, le
plan contient :

```text
N(2D+2) = 8 × (2×2+2) = 48 positions
```

La projection sur seulement deux valeurs par facteur crée des positions qui
désignent le même réglage final. Le starter contient **48 positions
statistiques mais 4 runs uniques** (deux taux par deux seeds). Ces duplications sont attendues dans cette
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

Les fichiers se trouvent sous `campaigns/mon-analyse-sa-sobol/`. Le fichier
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
| `seed` | `0.45` | `0.61` |

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

En option, dessiner les figures de l'analyse publiée :

```bash
"${SA[@]}" plot --meta-config "$TARGET"
```

Valides pour `sobol` : `output-summary` (une ligne par indicateur : médiane, q05 à q95 et min à max sur les positions du plan, jamais une incertitude du modèle), et `sobol-indices` (barres `first_order` et `total_order` avec l'IC bootstrap, jamais tronqué). La figure porte le bandeau « démonstration : un seul scramble, IC non inférentiels » : les intervalles ne sont pas inférentiels. Voir [Figures](workflow-and-results.md#figures).

## Adapter la démonstration

Vous pouvez tester une autre entrée scalaire du modèle, par exemple
`matching_minimum_observations: [5, 20, 50]`, dans une **nouvelle** campagne
(un nouveau nom `campaign`), après avoir écrit ce paramètre dans la section
`config:` de votre configuration eqasim. Le coût suit toujours `N(2D+2)`.

N'ajoutez `income_assignation_method` (`[uniform, bhepop2]`) que si vous
disposez d'un indicateur de revenu adapté : les trois indicateurs du starter ne
mesurent pas directement le revenu. Calculez une mesure de revenu avec un
[script d'analyse](analysis-scripts.md) qui lit `households` et lisez-la avec un
indicateur `value` (voir
[Vos propres calculs](yaml-reference.md#vos-propres-calculs)). N'ajoutez `hts` que si les deux jeux de données préparés existent, car ce
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

- La campagne est une démonstration (pas de bloc `charter`, ou `charter.status: demonstration`).
- `base_sample_size` est une puissance de deux d'au moins 2.
- `experiment.seeds` contient au moins deux entiers distincts.
- Les positions et les runs uniques sont distingués.
- Les indices hors plage éventuels restent visibles.
- Le résultat est présenté comme `demonstration_noninferential`.

## Suite

- Comparaison simple : [Grid](what-when-how-grid.md)
- Analyse scientifique : [Sobol multi-scramble](what-when-how-multiscramble_sobol.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
