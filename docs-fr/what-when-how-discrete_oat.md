# OAT discret : observer des changements locaux pas à pas

**Cas pratique : résumer l'effet local de `sampling_rate` sur deux voisinages.**

[English](../docs/what-when-how-discrete_oat.md) | **Français**

## En bref

OAT signifie « un facteur à la fois ». La méthode modifie un paramètre numérique
pendant que les autres restent fixes. Elle compare uniquement des valeurs
voisines : par exemple `0.001` à `0.002`, puis `0.002` à `0.003`.

Elle répond à une question locale et agrégée :

> En moyenne sur les voisinages visités, quels paramètres numériques produisent les plus grands changements de sortie ?

Cette méthode est utile pour une démonstration ou un premier diagnostic. Elle
n'est pas le Morris scientifique : ses pas n'ont pas tous la même taille et elle
ne fournit ni parts de variance ni preuve qu'un facteur peut être écarté.

## Différence avec Grid

Grid exécute toutes les combinaisons. OAT construit plutôt des chemins : à
chaque étape, un seul paramètre change. Avec plusieurs paramètres, cela coûte
moins cher qu'une grande grille, mais le résultat dépend des voisinages visités.

Avec un seul paramètre comme dans le cas ci-dessous, OAT sert surtout à apprendre
à lire un **effet local normalisé** : le changement de l'indicateur est divisé
par la taille relative du pas dans le domaine déclaré.

## Cas pratique de bout en bout

Ce guide suppose que vous avez déjà suivi les étapes de génération du
[cas Grid](what-when-how-grid.md#2-générer-le-projet-de-départ). Les commandes
sont lancées depuis `mon-analyse-sa/`, avec `$ROOT` et le tableau Bash `SA`
définis comme dans le [guide de démarrage](getting-started.md#installer-avec-uv).

### 1. Poser la question

Question : « En moyenne sur les passages `0.001 ↔ 0.002` et
`0.002 ↔ 0.003` sélectionnés par le design, `sampling_rate` produit-il un grand
changement local des distances aux références ? »

Nous observons les mêmes trois indicateurs que dans le [cas Grid](what-when-how-grid.md) :
âge contre INSEE, distance contre HTS et nombre de véhicules contre HTS.
Pour Nantes/EDGT 44, l'indicateur de distance est un diagnostic secondaire
asymétrique : les classes synthétiques utilisent la distance euclidienne entre
activités, tandis que HTS utilise la distance routée.

### 2. Utiliser le fichier généré

Après `eqasim-sa init`, le fichier rendu `configs/02-discrete-oat.yml` contient
déjà ce cas :

```yaml
charter:
  status: demonstration

experiment:
  method: discrete_oat
  seed_role: replication
  trajectories: 2
  design_seed: 20260817
  replications:
    seeds: [1301, 1302]

parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
```

Les autres blocs du fichier rendu définissent le modèle, les données, les trois
indicateurs, l'exécution locale et la calibration. Le fichier source
`src/eqasim_sa/templates/02-discrete-oat.yml` contient des placeholders ; c'est
la copie créée dans votre projet par `init` qu'il faut exécuter.

Les valeurs doivent être numériques, distinctes et strictement croissantes. La
seed reste une réplication dans `experiment.replications.seeds` ; ne déclarez
jamais `random_seed` comme paramètre d'analyse.

### 3. Comprendre la taille du plan

Le fichier déclare :

- `D = 1` paramètre ;
- `T = 2` trajectoires ;
- `R = 2` seeds de réplication.

Le plan contient donc `T(D+1)R = 2 × 2 × 2 = 8` positions. Certaines positions
peuvent désigner exactement le même réglage et la même seed. Le starter produit
ainsi normalement **8 positions statistiques mais 6 runs uniques**. Le simulateur
n'exécute pas deux fois un même `runId`.

### 4. Comprendre les pas comparés

L'étendue totale vaut `0.003 - 0.001 = 0.002` :

| Changement | Taille du pas | Fraction de l'étendue |
| --- | ---: | ---: |
| `0.001 → 0.002` | `0.001` | `0.5` |
| `0.002 → 0.003` | `0.001` | `0.5` |

Pour chaque pas, seed et indicateur, la méthode calcule le changement de sortie
divisé par la fraction signée du pas. Elle agrège ensuite ces effets dans **une
seule ligne par facteur et indicateur**. Le fichier final ne fournit donc pas une
ligne séparée pour `0.001 → 0.002` et une autre pour `0.002 → 0.003`. Cette
normalisation rend les effets comparables dans le domaine déclaré, mais ne les
transforme pas en parts de variance.

Si vous utilisez plutôt `[0.001, 0.002, 0.005]`, les fractions deviennent `0.25`
et `0.75`. Les deux effets n'ont alors pas été observés sur la même distance et
doivent rester interprétés comme des changements locaux.

### 5. Planifier, vérifier et exécuter

Depuis le projet généré, commencez par la calibration de ressources :

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml

TARGET=configs/02-discrete-oat.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

`check` est statique sauf avec `--smoke`. `analyze` calcule les indicateurs et
les effets ; il crée aussi le rapport gouverné.

### 6. Lire les résultats

Les résultats se trouvent sous `campaigns/discrete_oat/`. Le fichier principal
est `discrete-oat-effects.parquet`. Il contient une ligne par facteur et par
indicateur avec notamment :

| Colonne | Lecture simple |
| --- | --- |
| `mu` | effet local moyen avec son signe ; positif signifie que l'indicateur augmente lorsque le facteur augmente |
| `mu_star` | taille moyenne de l'effet sans annuler les signes opposés |
| `sigma` | écart-type de tous les effets élémentaires `T × R` ; il mélange la variation entre trajectoires et entre seeds |
| `seed_std` | racine de la variance moyenne entre seeds, calculée séparément dans chaque trajectoire |
| `rank` | ordre descriptif selon `mu_star` |

Supposons, dans une **illustration fictive**, que la ligne de l'indicateur âge
donne `mu_star = 0.012` et `seed_std = 0.010`. L'effet local moyen du taux est
alors à peine supérieur à la variation entre seeds. Cela invite à conserver le
paramètre sous surveillance ; cela ne démontre pas un effet global.

Si `mu` est proche de zéro mais `mu_star` est grand, les pas observés ont produit
des effets de signes opposés qui se compensent dans `mu`. Si `sigma` est grand,
la dispersion peut venir des trajectoires, des voisinages **ou des seeds** ;
comparez-la à `seed_std` avant de parler de non-linéarité locale.

Les autres fichiers servent à vérifier la lecture :

- `indicators.parquet` contient la valeur de chaque indicateur pour chaque run ;
- `replication-dispersion.parquet` montre la dispersion entre seeds ;
- `governed-report.json` porte `demonstration_noninferential` et `claimAuthorized: false`.

## Étendre à plusieurs paramètres

Vous pouvez, dans une nouvelle campagne, comparer par exemple :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
  matching_minimum_observations:
    values: [5, 20, 50]
  matching_minimum_age:
    values: [0, 5, 12]
```

Avec deux trajectoires et deux seeds, cela donne
`2 × (3 + 1) × 2 = 16` positions. Chaque clé doit déjà exister sous `config:`
dans le modèle scellé. OAT discret refuse les catégories et les listes
structurées ; utilisez [Grid](what-when-how-grid.md) pour celles-ci.

## Ce que vous pouvez conclure

Vous pouvez comparer l'**effet local agrégé** des facteurs sur les voisinages
sélectionnés dans la démonstration. Le fichier agrégé ne permet pas de classer
séparément les deux transitions de `sampling_rate`.

Vous ne pouvez pas dire qu'un petit effet local prouve qu'un facteur est sans
importance, car OAT ne couvre pas toutes les combinaisons ni toutes les
interactions. Vous ne pouvez pas parler de part de variance, de priorisation ou
de fixation des facteurs.

Pour décider scientifiquement quels facteurs doivent rester à l'étude, utilisez
[Morris canonique](what-when-how-canonical_morris.md).

## Checklist

- La question précise les sorties et les voisinages numériques étudiés.
- Chaque domaine contient au moins deux valeurs strictement croissantes.
- Les paramètres sont numériques ; aucune configuration structurée n'est utilisée.
- Le nombre de positions est calculé avant `plan`.
- Une configuration modifiée utilise un nouveau `campaign` et un nouveau `campaign_root`.
- Le résultat est présenté comme un tri descriptif local, pas comme un criblage scientifique.

## Suite

- Précédent : [Grid](what-when-how-grid.md)
- Suivant : [Morris canonique](what-when-how-canonical_morris.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
