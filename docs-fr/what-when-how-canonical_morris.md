# Morris canonique : décider quels facteurs garder

**Cas pratique : cribler deux réglages du matching de la population nantaise.**

[English](../docs/what-when-how-canonical_morris.md) | **Français**

## En bref

Supposons que deux paramètres soient encore incertains :
`matching_minimum_observations` et `matching_minimum_age`. Les simuler dans une
étude plus coûteuse est possible, mais vous voulez d'abord mesurer l'ampleur et
la stabilité de leurs effets locaux avant l'étape suivante.

Morris canonique répond à la question de **criblage** :

> Quels facteurs devons-nous conserver pour l'étude suivante ?

Le protocole Nantes concret utilise une règle conservatrice : il conserve les
deux facteurs par construction avec `screening_top_k: 2`. Il apprend leur ordre
et sa stabilité, mais ne prend pas une décision d'élimination entre eux. Une
autre étude avec plus de facteurs ou une coupure plus petite peut utiliser le
même cadre pour un criblage réellement sélectif.

Le résultat est volontairement prudent. Un facteur bien classé doit rester. Un
facteur mal classé n'est pas automatiquement sans effet et ne peut pas être fixé
sur la seule base de Morris. La fixation exige une analyse Sobol d'ordre total et
une tolérance pratique déclarée.

## Les notions indispensables

- Un **facteur** est un paramètre incertain que la campagne fait varier.
- Un **estimand** est la quantité précise que l'étude cherche à estimer.
- Une **trajectoire** est un chemin où un seul facteur change à chaque pas.
- Un **effet élémentaire** est le changement de l'indicateur produit par ce pas.
- Un **criblage** garde les facteurs qui demandent encore de l'attention ; il ne fixe aucun facteur.

Morris place d'abord chaque facteur sur une même échelle de probabilité de `0` à
`1`. Tous les facteurs utilisent alors le même pas `Delta`, puis ce pas est
converti vers leurs valeurs déclarées. L'utilisateur déclare `levels` ; le
logiciel calcule `Delta`. N'ajoutez pas de clé `delta` au YAML.

## Cas Nantes concret

Le dépôt contient un protocole complet dans
[`01-canonical-morris-pilot.yml`](../protocols/nantes-scientific/01-canonical-morris-pilot.yml).
Il décrit un cas réel et pratique, mais **aucun résultat empirique Nantes n'est
fourni par ce guide** : les nombres d'interprétation plus bas sont fictifs.

### 1. Poser la question décisionnelle

Le protocole demande quels réglages doivent rester à l'étude pour la synthèse de
population de Loire-Atlantique, avec INSEE RP 2022 et EDGT 44.

| Élément de la charte | Choix du cas Nantes | Pourquoi |
| --- | --- | --- |
| Objectif | `screening` | conserver les facteurs qui demandent encore de l'attention |
| Usage | `diagnostic` | comprendre le modèle, pas prédire une politique future |
| Estimand | TV âge moyenne sur les seeds du simulateur | séparer l'effet des facteurs du bruit propre au simulateur |
| Indicateur principal | `tv_distance(age, ref=INSEE)` | mesurer l'écart de distribution d'âge à INSEE |
| Dépendance | facteurs indépendants | condition requise par ce design en espace de probabilité |

La seed est ici une **réplication**. Pour chaque position de la trajectoire, le
simulateur est répété avec plusieurs seeds et l'étude vise leur moyenne
conditionnelle.

### 2. Déclarer les facteurs et leurs valeurs

Le protocole fait varier :

```yaml
parameters:
  matching_minimum_observations:
    values: [5, 20, 50]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Nantes protocol stress support around documented matching behavior and default 20
  matching_minimum_age:
    values: [0, 5, 12]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Nantes protocol stress support around documented matching behavior and default 5
```

`discrete_uniform` signifie que les trois valeurs d'un facteur ont la même
probabilité dans l'étude. Cela ne veut pas dire qu'elles sont également
probables dans la réalité par défaut : la provenance doit expliquer pourquoi ce
support et cette pondération répondent à la question étudiée.

- `matching_minimum_observations` est le nombre minimal d'observations exigé dans une cellule de donneurs.
- `matching_minimum_age` est l'âge minimal utilisé par la procédure de matching.
- `sampling_rate` reste fixé à `0.001` et fait l'objet d'un contrôle de robustesse séparé, car il change aussi la taille de population et le bruit numérique.

Les deux clés variées doivent déjà exister sous `config:` dans le modèle scellé.
Morris canonique refuse les facteurs qui sont des listes structurées, comme
plusieurs listes `matching_attributes` nommées.

### 3. Déclarer les sorties observées

Le protocole utilise :

```yaml
outputs:
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
  distance:
    category: Distance class
    simulated: Proportion_EQASIM
    references: [HTS]
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references: [HTS]
indicators:
  - tv_distance:
      output: age
      reference: INSEE
  - tv_distance:
      outputs: [distance, nbr_vehicle]
```

Le bloc `indicators` produit les trois mêmes indicateurs que la valeur par
défaut, mais les écrit explicitement, comme il sied à un protocole :
`tv_distance(age, ref=INSEE)`, `tv_distance(distance, ref=HTS)` et
`tv_distance(nbr_vehicle, ref=HTS)`. La décision de criblage est gouvernée par
l'indicateur âge principal. Les deux
indicateurs HTS restent des diagnostics secondaires. Une TV plus faible indique
une distribution plus proche de la référence, mais Morris classe l'ampleur du
**changement** de TV lorsque les facteurs changent.

Pour Nantes/EDGT 44, l'indicateur de distance est asymétrique : les classes
synthétiques utilisent la distance euclidienne entre activités, tandis que HTS
utilise la distance routée. Il reste donc un diagnostic secondaire à interpréter
avec cette limite.

### 4. Comprendre le pilote

Le pilote déclare :

```yaml
experiment:
  method: canonical_morris
  levels: 4
  trajectories: 24
  candidate_trajectories: 32
  design_seed: 20260820
  seeds: [45001, 45002]
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 999
  confidence_level: 0.95
  convergence_checkpoints: [8, 16, 24]
  screening_top_k: 2
```

Le protocole écrit explicitement toutes les valeurs d'analyse et la graine de
design, même celles qui égalent une valeur par défaut, pour que la déclaration
se lise seule. Les deux seeds sont des seeds de base de réplication.

SALib tire 32 trajectoires candidates et optimise localement une sélection de 24 ;
seules les occurrences retenues sont simulées. Le tirage se fait avec remise :
la sélection scellée peut contenir plusieurs fois le même chemin sur la grille
unitaire, avec une seed de simulation dérivée distincte pour chaque occurrence.
Avec `D = 2`, `T = 24` et `R = 2`, le pilote contient :

```text
T(D+1)R = 24 × 3 × 2 = 144 positions
```

Le pilote ne sert pas à conclure. Il mesure notamment le bruit entre seeds et
recommande combien de réplications seront nécessaires dans une **nouvelle**
campagne de production.

### 5. Rendre et exécuter le pilote

Cette procédure suppose que vous avez terminé l'installation et `init` du
[cas Grid](what-when-how-grid.md#2-générer-le-projet-de-départ), que `$ROOT` et
le tableau Bash `SA` sont encore définis, et que la calibration de ressources
`mon-analyse-sa/configs/00-calibration.yml` possède au moins un run complet.

Les fichiers sous `protocols/nantes-scientific/` sont des déclarations avec des
marqueurs machine. Ils ne peuvent pas être passés directement à `eqasim-sa`.
Depuis la racine `openmole-eqasim`, rendez le pilote avec les chemins de votre
machine :

```bash
mkdir -p rendered
uv run --project "$ROOT/eqasim-sa" --locked python \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/render_protocol.py" \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/01-canonical-morris-pilot.yml" \
  "$ROOT/rendered/canonical-morris-pilot.yml" \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/mon-analyse-sa/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/mon-analyse-sa/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-pilot"
```

Le renderer vérifie notamment les valeurs fixes du modèle et remplace les
marqueurs sans modifier la question scientifique.

Planifiez ensuite la campagne. Si vous possédez une campagne de calibration de
ressources complète et compatible, fournissez-la explicitement à `check` et
`estimate` :

```bash
PILOT="$ROOT/rendered/canonical-morris-pilot.yml"
CALIBRATION="$ROOT/mon-analyse-sa/configs/00-calibration.yml"

"${SA[@]}" plan --meta-config "$PILOT"
"${SA[@]}" check --meta-config "$PILOT" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" estimate --meta-config "$PILOT" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" run --meta-config "$PILOT"
"${SA[@]}" analyze --meta-config "$PILOT"
```

Sans observation de calibration compatible, `check` et `estimate` ne peuvent pas
chiffrer correctement les ressources. Suivez alors la procédure explicite de
votre site avant `run` ; ne confondez pas cette calibration de ressources avec
le pilote scientifique.

### 6. Passer du pilote à la production

Ouvrez
`$ROOT/mon-analyse-sa/campaigns/canonical-morris-pilot/replication-allocation.json`.
Continuez seulement si `status` vaut `recommended`. Les statuts `capped` et
`infeasible` imposent de revoir le budget ou la précision visée.

Le renderer de production lit cette recommandation et crée une autre campagne :

```bash
uv run --project "$ROOT/eqasim-sa" --locked python \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/render_protocol.py" \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/02-canonical-morris-production.yml" \
  "$ROOT/rendered/canonical-morris-production.yml" \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/mon-analyse-sa/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/mon-analyse-sa/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-production" \
  --pilot-campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-pilot"

PRODUCTION="$ROOT/rendered/canonical-morris-production.yml"
"${SA[@]}" plan --meta-config "$PRODUCTION"
"${SA[@]}" check --meta-config "$PRODUCTION" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" estimate --meta-config "$PRODUCTION" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" run --meta-config "$PRODUCTION"
"${SA[@]}" analyze --meta-config "$PRODUCTION"
```

Si le pilote recommande `R` réplications, la production contient `24 × 3 × R =
72R` positions. Le pilote reste intact : ne changez jamais sa phase ou ses seeds
pour le transformer en production.

### 7. Lire le résultat de production

Le fichier principal est `canonical-morris-ranking.parquet` :

| Colonne | Lecture simple |
| --- | --- |
| `mu_star` | ampleur moyenne des effets élémentaires |
| `mu_star_ci_low`, `mu_star_ci_high` | incertitude obtenue en rééchantillonnant des trajectoires entières |
| `rank` et son intervalle | position du facteur et stabilité de cette position |
| `top_k_probability` | fréquence à laquelle le facteur appartient aux `k` premiers lors du bootstrap |
| `seed_std` | variabilité des effets élémentaires entre réplications de seed, et non bruit brut de sortie |

Illustration **fictive** :

| Facteur | Rang | `mu_star` | Intervalle de rang | Probabilité top 2 |
| --- | ---: | ---: | ---: | ---: |
| `matching_minimum_observations` | `1` | `0.18` | `1-2` | `1.00` |
| `matching_minimum_age` | `2` | `0.11` | `1-2` | `1.00` |

Avec `screening_top_k: 2`, les deux facteurs restent dans l'étude. Le tableau ne
dit pas que le premier explique 18 % de la variance : `mu_star` est une ampleur
d'effet Morris, pas une part de variance. Comme ce protocole contient exactement
deux facteurs, leur probabilité top 2 vaut nécessairement `1.00` : ce seuil
conservateur ne peut en éliminer aucun. Les rangs, leurs intervalles et les
inversions servent ici à vérifier l'ordre et sa stabilité avant que FP compare
les deux facteurs. Une autre étude qui veut une coupure top 1 doit la déclarer
avant `plan`, pas la choisir après avoir vu le classement.

FP signifie **priorisation des facteurs** : l'étape Sobol suivante compare les
deux facteurs conservés pour savoir lequel apprendre plus précisément en premier.

Vérifiez ensuite :

- `canonical-morris-convergence.parquet` pour l'évolution aux checkpoints ;
- `canonical-morris-rank-reversals.parquet` pour les inversions de rang ;
- `noise-floor.parquet` pour le bruit brut entre seeds, notamment `intrinsic_sd` et `mcse` ;
- `convergence-evidence.parquet` pour le critère d'arrêt ;
- `governed-report.json` pour le statut final.

`pilot_only` interdit toute conclusion de criblage. `unstable` signifie que le
classement n'a pas convergé. `conditional` permet seulement un résultat interne
conditionnel à la charte. Le contrat actuel laisse les contrôles de robustesse en
attente et conserve `claimAuthorized: false` : il ne produit pas une revendication
finale autorisée.

## Ce que vous pouvez conclure

Après une production complète et convergée, vous pouvez dire quels facteurs sont
**conservés conditionnellement** pour l'étude suivante, dans le cadre, les
supports et l'indicateur déclarés.

Vous ne pouvez pas dire qu'un facteur non retenu est sans importance, qu'il peut
être fixé, ou que `mu_star` est une part de variance. Vous ne pouvez jamais tirer
une conclusion du pilote seul.

## Checklist

- La charte nomme la décision, l'estimand et l'indicateur principal.
- Chaque support et l'indépendance des facteurs possèdent une justification.
- `levels` est pair et au moins égal à 4 ; `Delta` n'est pas ajouté au YAML.
- Le pilote et la production ont des noms et des racines distincts.
- La production reprend uniquement une recommandation pilote `recommended`.
- Les critères de convergence et les contrôles de robustesse sont lus avant toute phrase de conclusion.

## Suite

- Démonstration locale : [OAT discret](what-when-how-discrete_oat.md)
- Priorisation ou fixation : [Sobol multi-scramble](what-when-how-multiscramble_sobol.md)
- Protocole complet : [Nantes scientifique](../protocols/nantes-scientific/README.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
