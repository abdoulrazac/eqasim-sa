# Paramètres et méthodes

[English](../docs/parameters-and-methods.md) | **Français**

## Domaine d'un facteur

Une valeur de facteur est exactement un entier signé 64 bits, un float fini ou
une chaîne non vide sans caractère NUL. Les booléens, `null`, tableaux, mappings,
NaN et infinis sont interdits. Toutes les valeurs d'un domaine ont exactement le
même type Python : `[1, 2]` et `[1.0, 2.0]` sont valides, `[1, 2.0]` ne l'est pas.

Un nom suit `^[A-Za-z_][A-Za-z0-9_]*$`, doit exister dans `config:` du YAML
eqasim et ne peut être réservé.

Noms de colonnes réservés : `designId`, `method`, `role`, `runId`, `seed`,
`contextHash`, `campaign`, `status`, `attempts`, `node`, `duration`,
`scratchBytes`.

Options d'infrastructure ou dédiées interdites comme facteurs : `data_path`,
`git_binary`, `java_binary`, `java_memory`, `maven_binary`, `output_path`,
`output_prefix`, `processes`, `random_seed`, `run`, `working_directory`.

Consultez la [procédure d'inspection du YAML modèle](yaml-reference.md#parameters)
pour lister les clés et leurs types avant de choisir les facteurs.

## Facteur scientifique ou override

Un facteur modifie le design, apparaît dans le plan et participe au `runId`.
Utilisez-le seulement si sa variation fait partie de la question scientifique.

Un override adapte l'environnement d'exécution. Il n'apparaît pas dans le plan
et n'entre pas dans le `runId`. Seules quelques options d'infrastructure sont
autorisées. Changer `sampling_rate` via `execution.overrides` est donc refusé;
le déclarer dans `parameters` est la voie correcte.

Les exclusions exactes de la configuration canonique sont `working_directory`
et, sous `config`, `data_path`, `git_binary`, `java_binary`, `java_memory`,
`maven_binary`, `output_path` et `processes`. `random_seed` est retirée de
`config` mais la seed est portée séparément dans l'enveloppe du `runId`.
`output_prefix` n'est pas exclu : sa valeur scellée affecte donc `runId`, même
s'il s'agit d'une option dédiée qui ne peut être ni facteur ni override.

## Schémas d'âge nommés

`eqasim-france` expose deux sélecteurs scalaires de type chaîne.
`matching_age_scheme` contrôle les classes d'âge du matching statistique et vaut
`matching_6` par défaut. `analysis_age_scheme` contrôle les classes des sorties
`age`, `age_purpose` et `travel` et vaut `analysis_7` par défaut.

| Schéma | Bornes du matching | Classes de sortie |
| --- | --- | --- |
| `matching_6` | `[14, 29, 44, 59, 74, 1000]` | `0-14`, `15-29`, `30-44`, `45-59`, `60-74`, `75+` |
| `analysis_7` | `[10, 14, 17, 24, 49, 64, 1000]` | `0-10`, `11-14`, `15-17`, `18-24`, `25-49`, `50-64`, `65+` |

Les plans Grid et Sobol générés font varier `matching_age_scheme` entre ces deux
noms. La configuration modèle conserve `analysis_age_scheme: analysis_7` fixe
afin que les indicateurs comparés gardent les mêmes classes de sortie. Morris ne
peut pas porter ce facteur catégoriel car il accepte uniquement des domaines
numériques ordonnés. Les noms de schéma sont des facteurs scalaires ordinaires
et participent donc au `runId`; les listes de bornes et autres recodages
structurés ne sont pas patchables par eqasim-sa.

`init` lit le YAML modèle sélectionné sans l'exécuter. Il exige les entrées
explicites `matching_age_scheme: matching_6` et
`analysis_age_scheme: analysis_7`. Cela fixe le matching historique pour la
calibration et Morris, stabilise les classes de sortie et rend l'identité des
runs générés explicite. Grid et Sobol remplacent toujours
`matching_age_scheme` via leur domaine de facteur.

## Catalogue des paramètres Nantes

Les paramètres suivants sont des candidats scientifiquement pertinents. Une clé
doit être explicitement présente sous `config:` dans le YAML modèle scellé avant
de pouvoir être déclarée comme facteur.

| Paramètre | Type et valeur actuelle | Effet | Domaine d'exemple | Méthodes |
| --- | --- | --- | --- | --- |
| `sampling_rate` | float, `0.001` | taille de population et bruit Monte Carlo | `[0.001, 0.002, 0.005]` | Grid, Morris, Sobol |
| `matching_age_scheme` | chaîne, `matching_6` | classes d'âge servant à sélectionner les donneurs HTS | `[matching_6, analysis_7]` | Grid, Sobol |
| `matching_minimum_observations` | entier, `20` | taille minimale d'une cellule de donneurs acceptée | `[5, 20, 50]` | Grid, Morris, Sobol |
| `matching_minimum_age` | entier, `5` | âge minimal des personnes cibles entrant dans le matching | `[0, 5, 12]` | Grid, Morris, Sobol |
| `income_assignation_method` | chaîne, `uniform` | méthode de synthèse du revenu, `uniform` ou `bhepop2` | `[uniform, bhepop2]` | Grid, Sobol |
| `hts` | chaîne, `edgt_44` | donneurs et distributions de référence de l'enquête | `[edgt_44, entd]` | Grid, Sobol, si les deux jeux existent |
| `projection_year` | entier si activé | population synthétique projetée; référence INSEE du starter inchangée | `[2020, 2030, 2040]` | conditionnel aux données et au scénario de projection |

Ces valeurs montrent la syntaxe; elles ne constituent pas automatiquement des
domaines scientifiquement justifiés. En particulier :

- augmenter `matching_minimum_observations` force un relâchement plus précoce
  vers moins d'attributs lorsque les cellules de donneurs sont petites ;
- `matching_minimum_age` change les personnes cibles recevant un donneur HTS,
  pas seulement les classes d'âge utilisées pour comparer les résultats ;
- `income_assignation_method: bhepop2` exige des distributions Filosofi valides
  par attribut et peut retomber sur `uniform` pour certaines communes ; la
  méthode effectivement appliquée n'est pas conservée dans la sortie actuelle,
  donc ce fallback doit être instrumenté avant une comparaison stricte ;
- le revenu est déjà archivé dans l'artefact `households`, mais le pack initial
  ne définit aucun scalaire de revenu ; enregistrez un indicateur personnalisé
  sur cet artefact avant d'interpréter ce facteur ;
- changer `hts` change à la fois les donneurs et les références HTS, et peut
  exiger un cache de données préparé séparément ;
- `projection_year` exige un `projection_scenario` compatible et l'archive de
  projection. Dans le starter actuel, `analysis.synthesis.population` lit
  toujours `data.census.filtered` pour la comparaison INSEE : n'affirmez pas que
  cette référence suit la synthèse projetée sans modifier et valider ce stage.

`matching_attributes` est important mais prend une liste ordonnée : le dernier
attribut est relâché en premier. Il n'est pas transportable actuellement comme
facteur eqasim-sa. `random_seed` ne se déclare jamais comme paramètre modèle
ordinaire; utilisez le mécanisme de seed propre à la méthode décrit plus bas.

### Exemple Grid concret

Ce design croise taille de population, recodage de l'âge et seuil de cellule de
donneurs. Deux seeds de réplication donnent `2 × 2 × 3 × 2 = 24` positions :

```yaml
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1201, 1202]
parameters:
  sampling_rate:
    values: [0.001, 0.002]
  matching_age_scheme:
    values: [matching_6, analysis_7]
  matching_minimum_observations:
    values: [5, 20, 50]
```

### Exemple Morris concret

Morris accepte uniquement des facteurs numériques ordonnés. Avec trois facteurs,
deux trajectoires et deux seeds de réplication, cet exemple contient
`2 × (3+1) × 2 = 16` positions :

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 2
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.003]
  matching_minimum_observations:
    values: [5, 20, 50]
  matching_minimum_age:
    values: [0, 5, 12]
```

### Exemple Sobol concret

Pour Sobol, la seed est elle-même un facteur. Ajouter le seuil de cellule aux
facteurs générés donne `D=4`; avec `N=64`, le plan Saltelli complet contient
`64 × (2×4+2) = 640` positions avant déduplication :

```yaml
experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 64
  design_seed: 20260814
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
  matching_age_scheme:
    values: [matching_6, analysis_7]
  matching_minimum_observations:
    values: [5, 20, 50]
  seed:
    values: [1401, 1402, 1403, 1404]
```

## Seed de réplication ou facteur

Avec `seed_role: replication`, Grid et Morris répètent une configuration
scientifique pour mesurer sa dispersion stochastique. La seed est dans
`experiment.replications.seeds` et n'est pas une colonne de facteur ordinaire.

Avec `seed_role: factor`, Sobol inclut la seed dans la décomposition de variance.
Elle est dans `parameters.seed.values`. Une seed ne peut jamais être facteur et
réplication dans la même campagne.

`design_seed` ne pilote pas eqasim. Elle rend déterministe le choix des
trajectoires Morris ou l'échantillon Sobol brouillé. Elle est indépendante de la
seed `random_seed` injectée dans chaque run.

## Grid

Grid exige au moins un facteur et accepte des domaines `int`, `float` ou
`string`, non vides et homogènes à la validation du plan.
Le parseur de plan autorise techniquement des niveaux dupliqués, mais le contrôle
`uniqueConfigSeedPairs` les rend invalides s'ils produisent la même
configuration et seed. Utilisez donc des niveaux uniques.

Pour des facteurs de tailles `L1...LD` et `R` seeds :

`positions = runs attendus = R × Π Li`

Exemple : deux taux, trois méthodes de revenu et quatre seeds donnent
`2 × 3 × 4 = 24` positions. Des configurations identiques après résolution
peuvent réduire les runs uniques; le `check` Grid les refuse.

## Morris

Morris accepte uniquement des facteurs numériques. Chaque domaine contient au
moins deux niveaux du même type, strictement croissants et distincts. L'ordre
est donc scientifique : il définit les pas adjacents utilisés pour les effets
élémentaires.

Avec `D` facteurs, `T` trajectoires et `R` seeds de réplication :

`positions = T(D+1)R`

Le nombre maximal de trajectoires candidates est :

`D! × Π 2(Li-1)`

Exemple : deux facteurs à trois niveaux, trois trajectoires et deux seeds donnent
`3 × (2+1) × 2 = 18` positions. Le plafond candidat vaut
`2! × 4 × 4 = 32` trajectoires.

Plusieurs positions peuvent partager un `runId`, notamment quand deux
trajectoires traversent le même point. Elles restent toutes dans le plan pour
préserver la géométrie statistique, mais le run n'est exécuté qu'une fois.

L'analyse classe par `mu_star` décroissant. `mu` conserve le signe moyen,
`sigma` décrit la dispersion des effets et `seed_std` agrège la variabilité entre
seeds au sein des trajectoires.

## Sobol

Chaque domaine Sobol contient au moins deux valeurs distinctes, homogènes, de
type `int`, `float` ou `string`. L'ordre des modalités est significatif. Pour un
tirage quasi-aléatoire `u` dans `[0,1)` et un domaine de longueur `L`, la modalité
retenue est celle d'indice `floor(u × L)`. La seed est toujours un facteur entier
spécial.

Avec `D` facteurs au total, seed comprise, et `N = base_sample_size` :

`positions = N(2D+2)`

`N` est une puissance de deux au moins égale à 2. `D` est compris entre 2 et
10 600 et `N(2D+2)` doit tenir dans un entier signé 64 bits.

Exemple : `sampling_rate`, `income_assignation_method` et `seed` donnent `D=3`.
Avec `N=64`, le plan contient `64 × 8 = 512` positions. La projection discrète
peut créer de nombreux doublons : le nombre de runs uniques peut être bien plus
petit que 512, sans invalider le plan Sobol.

Avant publication du plan, chaque facteur doit avoir au moins un contraste
hybride effectif après projection discrète : un `AB_i` projeté doit différer de
`A` ou le `BA_i` correspondant de `B`. Un facteur sans contraste est non
identifiable et refusé. Les positions dupliquées dans les autres lignes restent
valides et continuent à partager un run.

Les indices produits sont de premier ordre et d'ordre total. Le rang est trié
par indice total décroissant, puis par nom de facteur croissant en cas d'égalité.
Les intervalles sont obtenus par bootstrap déterministe; un indicateur constant
ou une variance non positive rend l'analyse invalide.

## Indicateurs et variation totale

Un indicateur transforme les artefacts d'un run en un `float` fini. Le
scalariseur intégré `tv_distance` compare deux distributions de masses après
normalisation séparée :

`TV(p,q) = 0.5 × Σ |p_i-q_i|`

La valeur 0 indique des distributions identiques; 1 indique des supports
totalement disjoints dans les classes fournies. Cette mesure n'établit pas à elle
seule la qualité causale ou scientifique d'un modèle : elle dépend des classes,
de la référence et des artefacts déclarés.

Voir le [glossaire](glossary.md) pour les distinctions entre facteur, override,
run, position et scalariseur.
