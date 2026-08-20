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
afin que les indicateurs comparés gardent les mêmes classes de sortie. L'OAT discret ne
peut pas porter ce facteur catégoriel car il accepte uniquement des domaines
numériques ordonnés. Les noms de schéma sont des facteurs scalaires ordinaires
et participent donc au `runId`; les listes de bornes et autres recodages
structurés ne sont pas patchables par eqasim-sa.

`init` lit le YAML modèle sélectionné sans l'exécuter. Il exige les entrées
explicites `matching_age_scheme: matching_6` et
`analysis_age_scheme: analysis_7`. Cela fixe le matching historique pour la
calibration et l'OAT discret, stabilise les classes de sortie et rend l'identité des
runs générés explicite. Grid et Sobol remplacent toujours
`matching_age_scheme` via leur domaine de facteur.

## Catalogue des paramètres Nantes

Les paramètres suivants sont des candidats scientifiquement pertinents. Une clé
doit être explicitement présente sous `config:` dans le YAML modèle scellé avant
de pouvoir être déclarée comme facteur.

| Paramètre | Type et valeur actuelle | Effet | Domaine d'exemple | Méthodes |
| --- | --- | --- | --- | --- |
| `sampling_rate` | float, `0.001` | taille de population et bruit Monte Carlo | `[0.001, 0.002, 0.005]` | Grid, OAT discret, Morris canonique, Sobol |
| `matching_age_scheme` | chaîne, `matching_6` | classes d'âge servant à sélectionner les donneurs HTS | `[matching_6, analysis_7]` | Grid, Sobol |
| `matching_minimum_observations` | entier, `20` | taille minimale d'une cellule de donneurs acceptée | `[5, 20, 50]` | Grid, OAT discret, Morris canonique, Sobol |
| `matching_minimum_age` | entier, `5` | âge minimal des personnes cibles entrant dans le matching | `[0, 5, 12]` | Grid, OAT discret, Morris canonique, Sobol |
| `income_assignation_method` | chaîne, `uniform` | méthode de synthèse du revenu, `uniform` ou `bhepop2` | `[uniform, bhepop2]` | Grid, Sobol |
| `hts` | chaîne, `edgt_44` | donneurs et distributions de référence de l'enquête | `[edgt_44, entd]` | Grid, Sobol, si les deux jeux existent |
| `projection_year` | entier si activé | population synthétique et référence INSEE projetées | `[2020, 2030, 2040]` | conditionnel aux données et au scénario de projection |

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
  projection. Sans ce paramètre, `analysis.synthesis.population` compare à
  `data.census.filtered`; avec lui, le stage compare à
  `synthesis.population.projection.reweighted`, donc INSEE suit le recensement
  projeté.

Le starter déclare exactement trois signaux TV : âge contre INSEE, distance
contre HTS brut et nombre de véhicules contre HTS brut. eqasim-france continue
de produire `HTS adjust with INSEE population weights` pour compatibilité, mais
cette colonne est une simple mise à l'échelle globale de HTS. La normalisation TV
annule cette échelle; les nouveaux starters ne la déclarent donc pas comme
seconde référence.

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

### Exemple OAT discret concret

L'OAT discret accepte uniquement des facteurs numériques ordonnés. Avec trois facteurs,
deux trajectoires et deux seeds de réplication, cet exemple contient
`2 × (3+1) × 2 = 16` positions :

```yaml
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

Avec `seed_role: replication`, Grid et OAT discret répètent une configuration
pour mesurer sa dispersion stochastique. Morris canonique considère plutôt les
valeurs comme des seeds de base et dérive une seed distincte pour chaque bloc
trajectoire-réplication. Dans les trois cas, la déclaration est dans
`experiment.replications.seeds` et n'est pas une colonne de facteur ordinaire.

Avec `seed_role: factor`, Sobol inclut la seed dans la décomposition de variance.
Elle est dans `parameters.seed.values`. Une seed ne peut jamais être facteur et
réplication dans la même campagne.

`design_seed` ne pilote pas eqasim. Elle rend déterministes les trajectoires ou
l'échantillon Sobol brouillé et, pour Morris canonique, participe à la dérivation
versionnée des seeds de run. Elle est distincte de la seed `random_seed` injectée.

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

## `morris` historique et OAT discret

L'ancien `method: morris` conserve exactement l'ancien schéma et l'ancienne
identité de plan afin que les plans existants restent lisibles comme provenance
de démonstration. Il n'est pas une méthode inférentielle actuelle et refuse une
charte scientifique. Les nouvelles démonstrations à niveaux adjacents utilisent
`method: discrete_oat`, avec la même forme d'expérience mais une identité de
méthode, un domaine de `designId` et des métadonnées séparés.

Les deux acceptent uniquement des facteurs numériques. Chaque domaine contient au
moins deux niveaux du même type, strictement croissants et distincts. L'ordre
définit les pas adjacents. Chaque effet divise la différence de sortie par la
fraction locale du pas dans l'étendue numérique déclarée. Ce sont des effets
normalisés locaux, pas des effets élémentaires Morris canoniques.

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

L'OAT discret est une méthode de démonstration. Il peut signaler des changements
locaux à conserver à l'étude, mais n'estime pas des parts de variance, n'établit
pas une importance globale, ne soutient pas FP et ne justifie pas FF.

## Morris canonique

Morris canonique est réservé à une campagne scientifique d'objectif `screening`,
avec dépendance des paramètres `independent` et, pour chaque paramètre, support
uniforme discret, transformation identité et provenance déclarés. Il utilise un
nombre pair `p >= 4` et le pas commun exact `Delta = p/[2(p-1)]` dans l'espace de
probabilité unitaire.

Un parcours déterministe seedé crée un pool d'au plus 500 trajectoires
candidates. La sélection prend d'abord la paire qui maximise la somme entière
des distances quadratiques entre points, puis ajoute gloutonnement le candidat
qui maximise la somme de ces distances aux trajectoires retenues. Les égalités
sont départagées par le plus petit identifiant lexicographique. Cette règle
`greedy-sum-pairwise-squared-distance` n'est ni maximin ni un optimum de
Campolongo. Un budget séparé de calcul des distances peut refuser un pool sous le
plafond de 500.

Les facteurs sont triés lexicalement dans le contrat scellé. Pour un support de
longueur `L`, l'indice de grille `z` se projette par inverse généralisée uniforme
discrète : indice 0 pour `z = 0`, sinon
`min((L*z - 1)//(p - 1), L - 1)`. Les seeds YAML sont des seeds de base. Une seed
signée 64 bits est dérivée par SHA-256 versionné pour chaque trajectoire et
réplication ; les `D+1` points du bloc partagent cette seed, ce qui fournit les
nombres aléatoires communs au contraste, et aucune seed dérivée ne se répète.

L'estimand est réplication d'abord : pour chaque trajectoire et facteur, les
effets sont moyennés sur les `R` réplications, puis `mu`, `mu_star` et `sigma`
sont calculés sur les `T` moyennes de trajectoire. La trajectoire est donc l'unité
effective du bootstrap, même si `elementary_effects` affiche `T*R`. `seed_std`
est la racine carrée de la moyenne des variances intra-trajectoire entre seeds ;
il vaut `null` avec une seule réplication.

Le classement suit `mu_star` décroissant. Les égalités exactes reçoivent le rang
moyen ; une égalité qui traverse la coupure top-k partage fractionnellement les
places restantes. Le bootstrap de trajectoires fournit une preuve d'incertitude
de design fini conditionnelle au contrat déclaré et scellé. Il ne fournit pas à
lui seul une preuve décisionnelle calibrée, des parts de variance, une conclusion
FP/FF, ni une certification automatique des critères d'arrêt et de robustesse.

## Sobol historique

Le `method: sobol` généré est une provenance historique de démonstration : un
scramble, `N(2D+2)` positions et un bootstrap déterministe des lignes. Le fichier
inchangé `sobol-indices.parquet` classe l'ordre total décroissant et fournit les
intervalles bootstrap. Son manifeste déclare
`inferenceStatus: legacy_noninferential`.

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

Le bootstrap de lignes d'un scramble ne remplace pas plusieurs scrambles QMC
indépendants. Un indicateur constant ou une variance non positive invalide
l'analyse.

## Sobol multi-scramble

`method: multiscramble_sobol` est la méthode scientifique FP/FF. Avec `S` seeds
de scramble, le checkpoint maximal `N` et `D` facteurs seed comprise, le plan a
`S*N(2D+2)` positions. Chaque seed produit un net SciPy Sobol indépendant à `N`;
les checkpoints inférieurs en sont des préfixes exacts, sans mélange des lignes.

Pour les `S` estimations indépendantes, les sorties donnent moyenne, SD
échantillonnal, `SE = SD/sqrt(S)` et intervalle bilatéral de Student à `S-1`
degrés de liberté. Aucun bootstrap de lignes n'est utilisé. FP classe la moyenne
de premier ordre décroissante. FF classe la borne supérieure d'ordre total
croissante. Pour cette méthode, `practical_tolerance` est une part de variance
d'ordre total, sans dimension et comprise dans `[0,1]`, interactions incluses.
La classification décisionnelle FF n'est produite que pour le
`primary_indicator` scellé. Les indicateurs secondaires conservent estimations et
rangs à titre diagnostique, mais ne peuvent porter aucune classification de
fixation. Pour l'indicateur primaire, la seed n'est jamais classée fixable.

Les clés d'analyse exactes sont `completeness_threshold: 1.0` et
`confidence_level`. Les quatre sorties sont `multiscramble-sobol-indices.parquet`,
`multiscramble-sobol-by-scramble.parquet`,
`multiscramble-sobol-convergence.parquet` et
`multiscramble-sobol-diagnostics.parquet`. Les limites sont 1 000 000 positions
et 20 000 000 cellules de table Arrow.

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
