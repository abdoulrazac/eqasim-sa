# OAT discret : quoi, quand et comment

**Utilisez OAT discret pour un tri descriptif local entre niveaux numériques adjacents.**

[English](../docs/what-when-how-discrete_oat.md) | **Français**

## Quoi

OAT signifie « un facteur à la fois ». La méthode suit de courts chemins. Un
facteur numérique change et tous les autres restent fixes. Chaque changement
utilise deux valeurs voisines déclarées.

Une seed est un entier qui sélectionne un flux aléatoire du simulateur. Les seeds
de réplication répètent chaque réglage pour montrer la variation aléatoire entre
seeds.

Elle répond à la question : « Quels changements locaux adjacents demandent plus
d'attention ? » C'est un tri descriptif local, pas un criblage scientifique.
Utilisez [Morris canonique](what-when-how-canonical_morris.md) pour un criblage
scientifique. Ses effets utilisent des pas locaux normalisés. Ce ne sont pas des
effets Morris canoniques.

## Quand

Utilisez-la pour vérifier le comportement local et trier des facteurs numériques
dans une démonstration.

Ne l'utilisez pas pour des textes, des configurations structurées nommées, des
interactions, des parts de variance, la priorisation ou la fixation des facteurs.
Une part de variance est la portion de variation de la sortie attribuée à une
entrée. Ne présentez pas son résultat comme un criblage scientifique.

## Exemples de paramètres

Ce fragment uniquement numérique utilise trois candidats réels d'eqasim-france.
Chaque clé doit déjà exister sous `config:` dans le YAML modèle scellé et chaque
domaine doit être strictement croissant :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
  matching_minimum_observations:
    values: [5, 20, 50]
  matching_minimum_age:
    values: [0, 5, 12]
```

Avec les `T=2` trajectoires, `D=3` facteurs et `R=2` seeds de réplication du
guide, le design a `T(D+1)R = 2 × 4 × 2 = 16` positions. Chaque pas a un sens
local simple :

- `sampling_rate` compare `0.001` à `0.002`, ou `0.002` à `0.005` ; ces pas
  valent respectivement `0.001/0.004 = 1/4` et `0.003/0.004 = 3/4` de l'étendue ;
- `matching_minimum_observations` compare les seuils de cellule `5` à `20`, ou
  `20` à `50` ; les fractions locales valent `1/3` et `2/3` de l'étendue ;
- `matching_minimum_age` compare les âges d'entrée `0` à `5`, ou `5` à `12` ;
  les fractions locales valent `5/12` et `7/12` de l'étendue.

La méthode divise chaque changement de sortie par cette fraction locale. Elle ne
compare pas toutes les paires et n'estime pas les interactions. Un facteur
structuré exigerait une clé de base de type liste, mais OAT discret le refuse.
Les seeds appartiennent uniquement à `experiment.replications.seeds` ; ne
déclarez jamais `random_seed` comme facteur. Ces domaines montrent la syntaxe et
l'interprétation, pas une justification scientifique automatique. Consultez le
[catalogue des paramètres Nantes](parameters-and-methods.md#catalogue-des-paramètres-nantes).

## Comment

Le YAML suivant est un **fragment**. Il ne s'exécute pas seul.

```yaml
charter:
  status: demonstration
experiment:
  method: discrete_oat
  seed_role: replication
  trajectories: 2
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
```

Partez du fichier généré complet [`02-discrete-oat.yml`](../src/eqasim_sa/templates/02-discrete-oat.yml).
Le projet généré définit `SA` comme un tableau de commande Bash. `"${SA[@]}"`
lance cette commande sans perdre ses arguments. Voir le [guide de démarrage](getting-started.md#runner-du-projet-généré).

Exécutez d'abord la calibration de ressources générée, comme expliqué dans le
[guide de démarrage](getting-started.md#première-calibration-des-ressources).
`check` et `estimate` ont besoin de ses observations de durée et de stockage.
Utilisez ensuite ces commandes. `plan` crée et scelle le plan. `check` valide le
plan et l'exécution. `estimate` donne le coût attendu en ressources. `run` exécute les runs
manquants. `analyze` crée les résultats.

```bash
TARGET=configs/02-discrete-oat.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## Ce que vous obtenez

- `indicators.parquet` : une ligne par run terminé et par indicateur.
- `replication-dispersion.parquet` : la variation entre les seeds de réplication.
- `discrete-oat-effects.parquet` : les effets locaux normalisés à niveaux adjacents.
- `output-summary.parquet` : un résumé de chaque indicateur sur les positions du design.
- `governed-report.json` : un rapport avec `inferenceStatus: demonstration_noninferential` et `claimAuthorized: false`.
- `analysis-generation.json` : les chemins et les empreintes de cette analyse.

`mu_star` résume la taille de l'effet. `mu` conserve sa direction. `sigma` montre
la variation entre les chemins. Ces valeurs décrivent seulement les pas locaux
déclarés.

## Ce que vous pouvez dire

Vous pouvez dire qu'un facteur a montré de grands ou de petits effets locaux dans
cette démonstration. Vous pouvez conserver ce facteur pour une étude ultérieure.

N'appelez pas cette méthode « criblage » sans le qualificatif « descriptif local ».
Vous ne pouvez pas dire qu'un facteur possède une part de variance, qu'il est peu
important globalement ou qu'il peut être fixé. La méthode change un facteur et
garde les autres fixes. Elle n'estime donc pas leurs effets combinés, appelés
interactions.

## Checklist avant exécution

- Vérifiez que la campagne est une démonstration.
- Utilisez seulement des facteurs numériques avec au moins deux valeurs distinctes.
- Placez les valeurs de chaque facteur dans un ordre strictement croissant.
- Vérifiez le nombre de trajectoires et la taille totale du design.
- Exécutez d'abord la calibration de ressources référencée.
- Utilisez un nouveau `campaign_root` après un changement de design ou de contexte.

## Suite

- Précédent : [Grid](what-when-how-grid.md)
- Suivant : [Morris canonique](what-when-how-canonical_morris.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
