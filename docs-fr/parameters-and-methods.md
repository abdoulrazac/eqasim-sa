# Paramètres et méthodes

[English](../docs/parameters-and-methods.md) | **Français**

## Domaine d'un facteur

Une valeur de facteur scalaire est exactement un entier signé 64 bits, un float
fini ou une chaîne non vide sans caractère NUL. Les booléens, `null`, tableaux,
mappings, NaN et infinis sont interdits comme niveaux scalaires. Les facteurs
listes structurées nommés sont l'exception explicite décrite plus bas. Toutes les
valeurs scalaires d'un domaine ont exactement le même type Python : `[1, 2]` et
`[1.0, 2.0]` sont valides, `[1, 2.0]` ne l'est pas.

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

## Facteur scientifique ou option machine

Un facteur modifie le design, apparaît dans le plan et participe au `runId`.
Utilisez-le seulement si sa variation fait partie de la question scientifique.

Une option machine (`execution.machine_options`) adapte l'environnement
d'exécution. Elle n'apparaît pas dans le plan et n'entre pas dans le `runId`.
Seules quelques options d'infrastructure sont autorisées. Changer
`sampling_rate` via `execution.machine_options` est donc refusé ; le déclarer
dans `parameters` est la voie correcte.

Les exclusions exactes de la configuration canonique sont `working_directory`
et, sous `config`, `data_path`, `git_binary`, `java_binary`, `java_memory`,
`maven_binary`, `output_path` et `processes`. `random_seed` est retirée de
`config` mais la seed est portée séparément dans l'enveloppe du `runId`.
`output_prefix` n'est pas exclu : sa valeur scellée affecte donc `runId`, même
s'il s'agit d'une option dédiée qui ne peut être ni facteur ni option machine.

## Catalogue des paramètres Nantes

Les paramètres eqasim suivants sont des candidats scientifiquement pertinents.
Une clé doit être explicitement présente sous `config:` dans la configuration
eqasim désignée par `model.config` avant de pouvoir être déclarée comme
facteur. Le `config.yml` officiel ne fixe que quelques clés, comme
`sampling_rate` ; écrivez d'abord les autres dans votre copie, avec leur valeur
par défaut dans eqasim. `hts` n'a pas de valeur par défaut dans eqasim et est
obligatoire : le `config.yml` officiel fixe `entd`, et le `config_nantes.yml`
officiel (donc votre copie `config_nantes_sa.yml`) fixe `edgt_44`.

| Paramètre | Type et valeur par défaut dans eqasim | Effet | Domaine d'exemple | Méthodes |
| --- | --- | --- | --- | --- |
| `sampling_rate` | float, `0.001` | taille de population et bruit Monte Carlo | `[0.001, 0.002, 0.005]` | Grid, OAT discret, Morris canonique, Sobol |
| `matching_minimum_observations` | entier, `20` | taille minimale d'une cellule de donneurs acceptée | `[5, 20, 50]` | Grid, OAT discret, Morris canonique, Sobol |
| `matching_minimum_age` | entier, `5` | âge minimal des personnes cibles entrant dans le matching | `[0, 5, 12]` | Grid, OAT discret, Morris canonique, Sobol |
| `income_assignation_method` | chaîne, `uniform` | méthode de synthèse du revenu, `uniform` ou `bhepop2` | `[uniform, bhepop2]` | Grid, Sobol |
| `hts` | chaîne, aucune valeur par défaut (`config.yml` officiel : `entd` ; Nantes : `edgt_44`) | donneurs et distributions de référence de l'enquête | `[edgt_44, entd]` | Grid, Sobol, si les deux jeux existent |

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
- le revenu est déjà archivé dans la sortie `households`, mais le pack initial
  ne définit aucun indicateur de revenu ; calculez la mesure de revenu dont vous
  avez besoin avec un [script d'analyse](analysis-scripts.md) qui lit
  `households`, et lisez-la avec un indicateur `value` avant d'interpréter ce
  facteur (voir [Vos propres calculs](yaml-reference.md#vos-propres-calculs)) ;
- changer `hts` change à la fois les donneurs et les références HTS, et peut
  exiger un cache de données préparé séparément.

Le fichier de départ déclare exactement trois signaux TV : âge contre INSEE, distance
contre HTS brut et nombre de véhicules contre HTS brut. eqasim-france continue
de produire `HTS adjust with INSEE population weights` pour compatibilité, mais
cette colonne est une simple mise à l'échelle globale de HTS. La normalisation TV
annule cette échelle; les nouveaux fichiers de départ ne la déclarent donc pas comme
seconde référence.

`matching_attributes` est important et ordonné : le dernier attribut est relâché
en premier. Déclarez des payloads de listes nommés et distincts sous
`configurations` ; le plan varie les labels et l'exécution reçoit la liste
ordonnée. Commencez par Grid. N'utilisez Sobol multi-scramble que si les
probabilités égales des labels et leur provenance sont scientifiquement
défendables. L'OAT discret et Morris canonique refusent ce facteur nominal. Archivez un
diagnostic `matched_counts` avant d'interpréter un changement de structure de
matching ; eqasim-sa ne l'implémente pas. `random_seed` reste propre à la méthode
et ne peut pas être structurée.

## Facteurs listes structurées nommés

Un facteur structuré associe chaque label sûr à une liste ordonnée non vide de
chaînes uniques et non vides. Labels et payloads sont uniques ; les alias de
listes identiques sont refusés. La clé du modèle scellé doit déjà être une liste
de chaînes. Le plan et l'analyse utilisent le support ordonné des labels, mais le
`runId` hache la liste complètement résolue. Seuls les plans structurés utilisent
le schéma v3 et scellent le mapping canonique exact. Les octets et schémas v1/v2
des plans non structurés restent inchangés.

### Exemple Grid concret

Ce design croise taille de population, méthode de synthèse du revenu et seuil
de cellule de donneurs. Deux seeds de réplication donnent `2 × 2 × 3 × 2 = 24` positions :

```yaml
experiment:
  method: grid
  seeds: [1201, 1202]
parameters:
  sampling_rate: [0.001, 0.002]
  income_assignation_method: [uniform, bhepop2]
  matching_minimum_observations: [5, 20, 50]
```

### Exemple OAT discret concret

L'OAT discret accepte uniquement des facteurs numériques ordonnés. Avec trois facteurs,
deux trajectoires et deux seeds de réplication, cet exemple contient
`2 × (3+1) × 2 = 16` positions :

```yaml
experiment:
  method: discrete_oat
  seeds: [1301, 1302]
  trajectories: 2
parameters:
  sampling_rate: [0.001, 0.002, 0.003]
  matching_minimum_observations: [5, 20, 50]
  matching_minimum_age: [0, 5, 12]
```

### Exemple Sobol concret

Pour Sobol, la seed est elle-même un facteur, construit à partir de
`experiment.seeds`. Faire varier le taux, la méthode de synthèse du revenu et
le seuil de cellule, plus le facteur seed, donne `D=4` ; avec `N=64`, le plan Saltelli complet contient
`64 × (2×4+2) = 640` positions avant déduplication :

```yaml
experiment:
  method: sobol
  seeds: [1401, 1402, 1403, 1404]
  base_sample_size: 64
parameters:
  sampling_rate: [0.001, 0.002, 0.005]
  income_assignation_method: [uniform, bhepop2]
  matching_minimum_observations: [5, 20, 50]
```

## Seed de réplication ou facteur

Les seeds se déclarent toujours dans `experiment.seeds` ; la méthode décide de
leur rôle.

Pour Grid et OAT discret, les seeds sont des réplications : chaque configuration
est répétée une fois par seed pour mesurer sa dispersion stochastique. Morris
canonique considère plutôt les valeurs comme des seeds de base et dérive une
seed distincte pour chaque bloc trajectoire-réplication. Dans ces trois cas, la
seed n'est pas une colonne de facteur ordinaire.

Pour Sobol et Sobol multi-scramble, les seeds forment le support d'un facteur
nommé `seed`, inclus dans la décomposition de variance ; il en faut au moins
deux. Un paramètre nommé `seed` sous `parameters` est refusé : une seed ne peut
donc jamais être facteur et réplication dans la même campagne.

`design_seed` ne pilote pas eqasim. Elle rend déterministes les trajectoires ou
l'échantillon Sobol brouillé et, pour Morris canonique, participe à la dérivation
versionnée des seeds de run. Elle est distincte de la seed `random_seed` injectée. Si elle est omise, elle
est dérivée du nom de campagne : renommer une campagne change donc son design
(voir [Valeurs par défaut](yaml-reference.md#valeurs-par-défaut-et-show-config)).

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

## OAT discret

Les démonstrations à niveaux adjacents utilisent `method: discrete_oat`, avec
les clés d'expérience `method`, `seeds`, `trajectories` et la clé facultative
`design_seed`.

L'OAT discret accepte uniquement des facteurs numériques. Chaque domaine contient au
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

SALib 1.5.2 génère l'échantillon candidat seedé et applique sa sélection Morris
publique avec optimisation locale et `optimal_trajectories` égal au nombre de
trajectoires déclaré. L'échantillonnage candidat se fait avec remise : sur une
petite grille finie, une même matrice peut donc être sélectionnée plusieurs fois.
Chaque occurrence reste un bloc de trajectoire explicite avec une seed dérivée
distincte ; les matrices et les options SALib effectives sont scellées dans les
métadonnées du plan. Un budget déterministe de calcul et le plafond de 500
bornent l'appel avant allocation.

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

Morris canonique utilise `stochastic.target: conditional_mean`. Les seeds de
réplication passent par la dérivation existante `canonical-morris-seed:v1` : tous
les rangs d'un bloc trajectoire/réplication partagent le même flux (CRN), tandis
que les blocs restent distincts. Pour chaque indicateur, trajectoire et rang,
`noise-floor.parquet` donne le nombre de réplications, la moyenne, la variance
intrinsèque non biaisée (unités de l'indicateur au carré), le SD intrinsèque et
`MCSE = SD/sqrt(R)` (unités de l'indicateur), ainsi que le marqueur d'indicateur
primaire. Les réplications décrivent le bruit; les trajectoires restent les unités
indépendantes du bootstrap.

Un pilote calcule le quantile linéaire NumPy par défaut `q` des variances
ponctuelles de l'indicateur primaire, puis
`R_brut = max(R_min, ceil(q / target_mcse^2))` et
`R_sélectionné = min(R_brut, R_max)`. `recommended` signifie que besoin brut et
positions structurelles tiennent; `capped` expose `R_brut > R_max`;
`infeasible` expose que `T*(D+1)*R_sélectionné` dépasse le plancher Decimal exact
du budget hors réserve. Aucun besoin n'est abaissé silencieusement. La sortie
spécifie une nouvelle campagne/racine de production immuable, avec provenance du
pilote non vide et exactement le nombre de seeds sélectionné.

## Sobol de démonstration

Le `method: sobol` généré est une démonstration non inférentielle : un
scramble, `N(2D+2)` positions et un bootstrap déterministe des lignes. Le fichier
inchangé `sobol-indices.parquet` classe l'ordre total décroissant et fournit les
intervalles bootstrap. Son manifeste déclare
`inferenceStatus: demonstration_noninferential`.

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
degrés de liberté. Aucun bootstrap de lignes n'est utilisé.
Avant l'appel à SALib, les tableaux finis d'indicateur sont centrés et mis à
l'échelle selon leur étendue observée. Cela préserve les indices sans dimension
lors d'un changement commun d'échelle de sortie et évite les overflows
intermédiaires évitables. L'analyse est refusée si cette mise à l'échelle efface
la variance de base, si un indice devient non fini, ou si la variance de sortie
dans les unités carrées d'origine sort de la plage représentable en `float64`.
Les valeurs et indices ne sont jamais tronqués pour forcer une publication. FP
classe la moyenne de premier ordre décroissante. FF classe la borne supérieure d'ordre total
croissante. `seed` est une nuisance finie déclarée : ses estimations restent
diagnostiques, `is_actionable` vaut faux et son rang comme sa tolérance FF sont
nuls. Ce support n'identifie ni bruit intrinsèque ni facteur contrôlable. Pour
cette méthode, `practical_tolerance` est une part de variance
d'ordre total, sans dimension et comprise dans `[0,1]`, interactions incluses.
La classification décisionnelle FF n'est produite que pour le
`primary_indicator` scellé. Les indicateurs secondaires conservent estimations et
rangs à titre diagnostique, mais ne peuvent porter aucune classification de
fixation. Seuls les facteurs ordinaires actionnables de l'indicateur primaire
sont classifiés.

Les clés d'analyse permises sont `completeness_threshold`, qui doit valoir
`1.0` (la valeur par défaut), et `confidence_level` (par défaut `0.95`). Les quatre sorties sont `multiscramble-sobol-indices.parquet`,
`multiscramble-sobol-by-scramble.parquet`,
`multiscramble-sobol-convergence.parquet` et
`multiscramble-sobol-diagnostics.parquet`. Les limites sont 1 000 000 positions
et 20 000 000 cellules de table Arrow.

## Indicateurs et variation totale

Un indicateur transforme les sorties d'un run en un `float` fini. L'indicateur
intégré `tv_distance` compare deux distributions de masses après
normalisation séparée :

`TV(p,q) = 0.5 × Σ |p_i-q_i|`

La valeur 0 indique des distributions identiques; 1 indique des supports
totalement disjoints dans les classes fournies. Cette mesure n'établit pas à elle
seule la qualité causale ou scientifique d'un modèle : elle dépend des classes,
de la référence et des sorties déclarées.

Voir le [glossaire](glossary.md) pour les distinctions entre facteur, option
machine, run, position et indicateur.
