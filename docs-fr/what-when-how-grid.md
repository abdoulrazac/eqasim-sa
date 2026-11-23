# Grid : comparer quelques réglages pas à pas

**Cas pratique : vérifier si `sampling_rate` modifie les sorties du modèle.**

[English](../docs/what-when-how-grid.md) | **Français**

## En bref

Supposons que vous hésitiez entre deux taux d'échantillonnage : `0.001` et
`0.002`. Grid exécute chaque taux avec plusieurs seeds du simulateur. Vous pouvez
alors comparer les sorties entre les deux taux et les variations entre seeds.

Grid est la méthode la plus simple pour commencer parce qu'elle teste toutes les
combinaisons que vous écrivez. Elle répond à une question descriptive :

> Qu'avons-nous observé pour chaque réglage testé ?

Elle ne répond pas à la question « quelle part de l'incertitude vient de chaque
paramètre ? » et ne prouve pas qu'un paramètre est important dans tous les cas.

## Quatre mots utiles

- Un **paramètre** est une entrée que l'on fait varier, ici `sampling_rate`.
- Une **seed** est un entier qui contrôle un tirage aléatoire du simulateur.
- Une **sortie** est une table produite par eqasim, par exemple la distribution des âges.
- Un **indicateur** résume une sortie par un nombre que l'on peut comparer entre runs.

Dans ce guide, l'indicateur est une distance de variation totale, notée TV. Elle
compare une distribution synthétique à une référence : `0` signifie qu'elles
sont identiques et `1` qu'elles ne se recouvrent pas. Une valeur plus faible
indique donc une meilleure proximité pour cet indicateur, sans résumer à elle
seule toute la qualité du modèle.

## Cas pratique de bout en bout

### 1. Poser la question

Question : « Passer de `sampling_rate: 0.001` à `0.002` change-t-il les
distributions synthétiques d'âge, de distance de déplacement et de nombre de
véhicules ? »

Le taux `0.001` tire environ 0,1 % de la population et `0.002` environ 0,2 %.
Augmenter ce taux peut réduire le bruit d'échantillonnage, mais augmente aussi le
temps de calcul. Nous gardons le modèle, les données et les autres paramètres
fixes pour rendre la comparaison lisible.

Cette petite campagne reste une **démonstration**. Elle vérifie le workflow et
montre ce qui s'est produit pour ces deux valeurs ; elle ne constitue pas une
analyse de sensibilité scientifique.

### 2. Générer le projet de départ

Suivez d'abord l'[installation](getting-started.md#installer-avec-uv), puis lancez
depuis la racine `openmole-eqasim` :

```bash
"${SA[@]}" init \
  --project-name mon-analyse-sa \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
cd mon-analyse-sa
```

`init` crée un fichier **rendu et utilisable** `configs/01-grid.yml`. Le fichier
du code source `src/eqasim_sa/templates/01-grid.yml` contient des valeurs
à remplacer (que `init` remplit) ; ne l'exécutez pas directement.

### 3. Déclarer le paramètre et les réplications

Pour isoler l'effet de `sampling_rate`, dérivez une nouvelle campagne à partir
du fichier généré et donnez-lui un nouveau nom :

```bash
cp configs/01-grid.yml configs/01-grid-sampling-rate.yml
```

Dans `configs/01-grid-sampling-rate.yml`, remplacez les blocs correspondants par :

```yaml
campaign: mon-analyse-sa-grid-sampling-rate
experiment:
  method: grid
  seeds: [1201, 1202, 1203]
parameters:
  sampling_rate: [0.001, 0.002]
```

Par rapport au fichier de départ, seuls le nom de campagne et la troisième seed changent.
Le reste du fichier généré, notamment `outputs`, `calibration`, `model` et `execution`,
reste inchangé. Le nouveau nom de `campaign` donne aussi à la campagne son
propre dossier, `campaigns/mon-analyse-sa-grid-sampling-rate/`. Pour vérifier la
configuration complète, valeurs par défaut comprises, lancez
`"${SA[@]}" show-config configs/01-grid-sampling-rate.yml`.

Votre configuration eqasim doit contenir `config.sampling_rate` (le
`config.yml` officiel le contient) et lister `analysis.synthesis.population`
dans `run:` (voir
[Préparer votre configuration eqasim](getting-started.md#préparer-votre-configuration-eqasim)).
Les seeds ne sont pas
des valeurs de `sampling_rate` : elles répètent chaque taux pour rendre visible
la variabilité aléatoire. Le plan contient ici :

```text
2 taux × 3 seeds = 6 positions et 6 runs uniques
```

### 4. Choisir ce que l'on mesure

Le fichier généré déclare déjà les sorties à conserver de chaque run. Les
fichiers de population (`persons`, `households`, `trips`, `activities` et les
activités spatiales `activities_spatial`) sont archivés pour qu'une analyse
ultérieure n'exige aucune nouvelle simulation ; ils n'ont pas de référence et ne
produisent donc aucun indicateur. Trois distributions sont comparées aux
données de référence :

```yaml
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
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
```

Il n'y a pas de bloc `indicators` : eqasim-sa calcule donc par défaut la
distance de variation totale (`tv_distance`) de chaque sortie contre chacune de
ses références. On obtient trois nombres par run :

| Nom produit | Ce qu'il compare |
| --- | --- |
| `tv_distance(age, ref=INSEE)` | âges synthétiques et âges INSEE |
| `tv_distance(distance, ref=HTS)` | distances synthétiques et enquête déplacements |
| `tv_distance(nbr_vehicle, ref=HTS)` | nombres de véhicules synthétiques et enquête déplacements |

Pour Nantes/EDGT 44, la comparaison de distance est un diagnostic secondaire
asymétrique : les classes synthétiques utilisent la distance euclidienne entre
activités, tandis que HTS utilise la distance routée.

Pour n'en garder que certains, ajoutez un bloc `indicators` (voir la
[référence YAML](yaml-reference.md#indicators)). Choisir un paramètre sans
choisir un indicateur ne suffit pas : on ne peut pas
parler de « l'effet de `sampling_rate` » sans préciser **sur quelle sortie**.

### 5. Calibrer les ressources, planifier et exécuter

La calibration mesure ici le temps et l'espace disque nécessaires. Elle ne
calibre pas les paramètres scientifiques du modèle.

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml

TARGET=configs/01-grid-sampling-rate.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

Les commandes ont des rôles différents :

| Commande | Rôle |
| --- | --- |
| `plan` | écrit les six positions dans `plan.parquet` et les scelle |
| `check` | vérifie statiquement la configuration, le plan, l'environnement et l'espace disponible |
| `estimate` | estime le temps et le stockage à partir de la calibration |
| `run` | exécute les runs qui manquent |
| `analyze` | calcule les indicateurs et crée le rapport ; il n'existe pas de commande `report` séparée |

`check` sans `--smoke` n'exécute pas un vrai run cible. Utilisez
`check --smoke` seulement si vous voulez explicitement ajouter deux runs
temporaires de validation.

### 6. Lire les résultats

L'état de campagne se trouve sous `campaigns/mon-analyse-sa-grid-sampling-rate/` :

- `plan.parquet` relie chaque `runId` à son taux et à sa seed ;
- `analysis-generation.json` désigne la génération immuable courante sous
  `analysis/generations/<generationId>/` ;
- le fichier `indicators.parquet` de cette génération contient les trois valeurs TV de chaque run ;
- son `replication-dispersion.parquet` résume la variation entre seeds pour chaque taux ;
- son `output-summary.parquet` résume l'ensemble du design ;
- son `governed-report.json` confirme `demonstration_noninferential` et
  `claimAuthorized: false`.

Le script suivant affiche la moyenne observée par taux et par indicateur :

```bash
uv run --project "$ROOT/eqasim-sa" --locked python - <<'PY'
from collections import defaultdict
import json
from pathlib import Path
from statistics import fmean

import pyarrow.parquet as pq

root = Path("campaigns/mon-analyse-sa-grid-sampling-rate")
manifest = json.loads((root / "analysis-generation.json").read_text())
plan = {row["runId"]: row for row in pq.read_table(root / "plan.parquet").to_pylist()}
groups = defaultdict(list)

indicators = root / manifest["outputs"]["indicators"]["path"]
for row in pq.read_table(indicators).to_pylist():
    rate = plan[row["runId"]]["sampling_rate"]
    groups[(rate, row["indicator"])].append(row["value"])

for (rate, indicator), values in sorted(groups.items()):
    print(rate, indicator, f"moyenne={fmean(values):.4f}", f"min={min(values):.4f}", f"max={max(values):.4f}")
PY
```

Voici une **illustration fictive**, uniquement pour apprendre à lire le tableau :

| `sampling_rate` | TV âge moyenne | Min-max entre seeds |
| ---: | ---: | ---: |
| `0.001` | `0.086` | `0.083-0.090` |
| `0.002` | `0.082` | `0.080-0.084` |

Dans cet exemple fictif, la différence des moyennes vaut `0.004`. Elle est du
même ordre que la variation entre seeds. La formulation prudente serait : « Sur
les valeurs et seeds testées, le taux `0.002` donne une TV âge moyenne légèrement
plus faible, mais la différence est petite au regard de la variabilité observée. »
Il faudrait davantage de réplications et un design scientifique déclaré pour
aller au-delà de cette observation.

En option, dessiner les figures de l'analyse publiée :

```bash
"${SA[@]}" plot --meta-config "$TARGET"
```

Valides pour `grid` : `output-summary` (une ligne par indicateur : médiane, q05 à q95 et min à max sur les positions du plan, jamais une incertitude du modèle), et `grid-response` (la moyenne publiée par valeur de chaque paramètre numérique, une ligne par combinaison des autres paramètres, avec l'IC à 95 % entre réplications quand les deux bornes existent ; ignorée si aucun paramètre n'est numérique). Les deux décrivent seulement les valeurs et les seeds observées. Voir [Figures](workflow-and-results.md#figures).

## Ce que vous pouvez conclure

Vous pouvez décrire les différences observées entre `0.001` et `0.002` pour les
trois indicateurs et les trois seeds déclarées.

Vous ne pouvez pas dire que `sampling_rate` explique une part globale de la
variance, qu'il est le paramètre le plus important, ou que `0.002` est optimal.
Grid n'explore que les combinaisons écrites dans le fichier.

## Adapter ce cas

- Pour comparer des seuils de matching, écrivez `matching_minimum_observations` dans la section `config:` de votre configuration eqasim, puis remplacez le paramètre par `matching_minimum_observations: [5, 20, 50]`.
- Pour croiser deux paramètres, ajoutez le second sous `parameters` et multipliez les tailles de domaines par le nombre de seeds.
- Pour comparer des listes `matching_attributes`, suivez le [tutoriel des configurations structurées](structured-configurations-tutorial.md).
- Pour un tri numérique local, continuez avec [OAT discret](what-when-how-discrete_oat.md).
- Pour un criblage scientifique, utilisez [Morris canonique](what-when-how-canonical_morris.md).

## Checklist

- La question nomme le paramètre, les valeurs et chaque sortie étudiée.
- Chaque clé de paramètre existe déjà sous `config:` dans votre configuration eqasim, et sa liste `run:` contient les étapes qui écrivent les sorties déclarées.
- Les seeds sont dans `experiment.seeds`, jamais sous `parameters`.
- Le nombre de positions reste compatible avec le budget.
- Une campagne modifiée possède un nouveau nom `campaign`, donc un nouveau dossier.
- Les résultats sont décrits comme une démonstration, pas comme une preuve scientifique.

## Suite

- Précédent : [Démarrage](getting-started.md)
- Suivant : [OAT discret](what-when-how-discrete_oat.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
