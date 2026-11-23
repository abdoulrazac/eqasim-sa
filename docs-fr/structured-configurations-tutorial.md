# Tutoriel des configurations structurées nommées

[English](../docs/structured-configurations-tutorial.md) | **Français**

Ce tutoriel compare trois listes ordonnées `config.matching_attributes` dans une
petite campagne Grid. Il démontre de bout en bout la configuration, l'exécution,
le stockage et l'analyse. Ce n'est pas une campagne scientifique et ses sorties
ne soutiennent aucune revendication inférentielle ou décisionnelle.

## 1. Préparer la configuration eqasim avant l'initialisation

Travaillez sur une copie de la configuration officielle de Nantes
`config_nantes.yml`, fournie par eqasim, placée dans le dossier eqasim, pour
laisser le fichier officiel intact :

```bash
cd "$ROOT/eqasim-france"
cp config_nantes.yml config_nantes_sa.yml
cd "$ROOT"
```

Dans `config_nantes_sa.yml`, écrivez `config.matching_attributes` sous forme de
liste de chaînes, et vérifiez que `run:` contient
`analysis.synthesis.population`, qui écrit les sorties `age`, `distance` et
`nbr_vehicle` comparées par les fichiers de départ :

```yaml
run:
  - synthesis.output
  - analysis.synthesis.population
config:
  matching_attributes:
    - sex
    - any_cars
    - age_class
    - socioprofessional_class
    - departement_id
```

Gardez les autres clés de la copie telles quelles. Faites-le avant `init` ou
tout `plan`. Git n'est pas nécessaire. Le contenu de ce fichier, comme le code
d'eqasim, fait partie de l'identité de la campagne : après le scellement d'un
plan, sa modification impose un nouveau projet ou, au minimum, un nouveau
`campaign` (donc un nouveau dossier de campagne vide). Ne recouvrez jamais une
campagne existante avec un contexte modifié.

## 2. Initialiser ou rejoindre un projet généré

Initialisez avec cette configuration ; `--model-config` est lu depuis le
dossier eqasim :

```bash
ROOT="$(pwd -P)"
SA=(uv run --project "$ROOT/eqasim-sa" --locked eqasim-sa)
PY=(uv run --project "$ROOT/eqasim-sa" --locked python)
"${SA[@]}" init \
  --project-name matching-structures \
  --model-root "$ROOT/eqasim-france" \
  --model-config config_nantes_sa.yml \
  --data-path "$ROOT/eqasim-data"
cd "$ROOT/matching-structures"
```

Les fichiers générés contiennent alors `config: config_nantes_sa.yml` sous `model`.
Un projet généré existant convient aussi si son `model.config` désigne cette
configuration et si aucune racine de campagne n'est réutilisée après le changement de
contexte. Utilisez le tableau `SA=(...)` de son `README.md` s'il diffère.
Pour les extraits d'inspection, définissez `PY=(python)` si eqasim-sa et PyArrow
sont installés dans l'environnement Python actif.

La cible référence la calibration des ressources. Si elle n'a pas déjà été
exécutée :

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml
```

## 3. Dériver une cible Grid séparée

Conservez le fichier de départ généré intact :

```bash
cp configs/01-grid.yml configs/04-structured-grid.yml
```

Dans `configs/04-structured-grid.yml`, effectuez exactement ces remplacements :

1. Remplacez la valeur de `campaign:` par `matching-structures-grid-structured`.
   Le dossier de campagne suit par défaut :
   `campaigns/matching-structures-grid-structured/`.
2. Remplacez tout le bloc `parameters:`, jusqu'à `outputs:` exclu, par le bloc
   ci-dessous.

```yaml
# structured-grid-tutorial-replacement
campaign: matching-structures-grid-structured
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
```

La ligne `campaign` remplace la ligne de premier niveau existante ; n'en ajoutez
pas une seconde copie. Le bloc `parameters` remplace le facteur du fichier de départ
(`sampling_rate`). Gardez tous les autres blocs de `01-grid.yml`, notamment :

- `method: grid` et les deux seeds de réplication `[1201, 1202]` ;
- les `outputs` : les fichiers de population conservés avec chaque run, et les
  trois distributions qui donnent par défaut les indicateurs âge/INSEE,
  distance/HTS et nombre de véhicules/HTS ;
- le bloc `calibration.meta_configs` ;
- les blocs `model` et `execution`, y compris `machine_options`.

`"${SA[@]}" show-config configs/04-structured-grid.yml` affiche la configuration
obtenue, toutes valeurs par défaut écrites.

Le plan comporte `3 configurations × 2 seeds de réplication = 6` positions et,
les trois charges ordonnées étant distinctes, 6 runs uniques attendus. La
variante `age_priority` réutilise les mêmes colonnes EDGT/recensement que la
variante par défaut mais change leur priorité de relâchement ; elle ne suppose
aucun champ indisponible.

## 4. Planifier, vérifier, chiffrer, exécuter et analyser

```bash
TARGET=configs/04-structured-grid.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

Au premier plan, vérifiez `designPositions: 6` et `uniqueRuns: 6`. `check` et
`estimate` exigent au moins un run complet dans la calibration référencée.

## 5. Inspecter le plan scellé avec PyArrow

Depuis le projet généré, ce script utilise PyArrow et la bibliothèque standard,
sans pandas :

```bash
"${PY[@]}" - <<'PY'
import json
import pyarrow.parquet as pq

path = "campaigns/matching-structures-grid-structured/plan.parquet"
plan = pq.read_table(path)
metadata = dict(plan.schema.metadata or {})

assert metadata[b"planSchemaVersion"] == b"3"
assert metadata[b"structuredFactorsVersion"] == b"1"
raw = metadata[b"structuredFactors"]
structured = json.loads(raw)
canonical = json.dumps(
    {name: structured[name] for name in sorted(structured)},
    separators=(",", ":"),
    ensure_ascii=False,
).encode("utf-8")
assert raw == canonical

print(plan.select(["runId", "seed", "matching_attributes"]).to_pylist())
print(json.dumps(structured, indent=2, ensure_ascii=False))
PY
```

La colonne `matching_attributes` contient les labels lisibles `default`,
`reduced` et `age_priority`. La métadonnée canonique `structuredFactors` les lie aux
listes ordonnées exactes. Les labels ne sont pas les valeurs d'exécution : le
`runId` est calculé sur la configuration entièrement résolue, liste ordonnée
comprise, et le simulateur reçoit cette liste dans son snapshot YAML par run.
Elle n'est volontairement pas transportée comme option CLI scalaire
`--matching_attributes`.

L'ordre a un sens pour le modèle : le matching eqasim relâche d'abord le dernier
attribut. Réordonner une charge modifie donc le comportement et le `runId`.

## 6. Joindre les indicateurs aux alias lisibles

La génération désignée par `analysis-generation.json` contient
`indicators.parquet`, avec le `runId`, le nom d'indicateur et sa valeur. Résolvez
son chemin publié et faites la jointure sans pandas :

```bash
"${PY[@]}" - <<'PY'
import json
from pathlib import Path

import pyarrow.parquet as pq

root = Path("campaigns/matching-structures-grid-structured")
manifest = json.loads((root / "analysis-generation.json").read_text())
plan = pq.read_table(root / "plan.parquet").select(
    ["runId", "seed", "matching_attributes"]
)
indicators = pq.read_table(root / manifest["outputs"]["indicators"]["path"])
joined = indicators.join(plan, keys="runId", join_type="left outer")
order = [
    ("matching_attributes", "ascending"),
    ("seed", "ascending"),
    ("indicator", "ascending"),
]
print(joined.sort_by(order).to_pylist())
PY
```

Interprétez étroitement les fichiers d'analyse :

- `replication-dispersion.parquet` regroupe chaque configuration nommée et
  indicateur sur les deux seeds et rapporte moyenne, écart-type, CV et intervalle
  normal simple ;
- `output-summary.parquet` résume toutes les observations planifiées ; ce n'est
  pas une estimation d'effet du facteur structuré ;
- `governed-report.json` classe cette campagne
  `demonstration_noninferential`, sans revendication autorisée.

Ce sont des comparaisons de démonstration. Grid n'estime aucune part de variance
de premier ordre ou totale et ne démontre ni FP ni FF. Un diagnostic
`matched_counts` par configuration est recommandé avant d'interpréter la
structure du matching, mais eqasim-sa ne calcule ni n'archive actuellement
`matched_counts`; organisez séparément cette preuve dans le workflow modèle.

## 7. Fragment scientifique multi-scramble

Un facteur structuré peut entrer dans une campagne FP/FF scientifique séparément
rédigée avec `method: multiscramble_sobol`. Ce qui suit n'est qu'un fragment de
paramètres, ni une charte scientifique complète ni une méta-configuration
exécutable :

```yaml
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Justification scientifique de probabilités égales sur trois structures de matching
experiment:
  method: multiscramble_sobol
  seeds: [44011, 44012, 44013, 44014]
  seeds_provenance: Support fini déclaré de nuisance des seeds du simulateur
```

Le support fini des seeds s'écrit dans `experiment.seeds`, jamais sous
`parameters` ; `seeds_provenance` indique d'où il vient. Le fragment omet les
autres clés d'expérience requises (`base_sample_sizes`, `scramble_seeds`).

Ici, `discrete_uniform` donne la même probabilité aux trois labels; il ne donne
pas une probabilité indépendante aux attributs internes. Le support fini de seed
est une nuisance non actionnable, pas le bruit intrinsèque non borné du
simulateur. Une vraie campagne exige encore les contrats complets de décision,
d'incertitude, d'estimand, de budget, de convergence, de robustesse, de
`finite_seed_factor`, de scrambles indépendants et de provenance. Ne les inventez
pas à partir de ce fragment. Consultez la
[référence YAML](yaml-reference.md), les
[paramètres et méthodes](parameters-and-methods.md#facteurs-listes-structurées-nommés)
et le [protocole nantais](../protocols/nantes-scientific/README.md) rédigé pour
l'étude.

## Refus et reprise sûre

- `discrete_oat` et `canonical_morris` refusent tous deux les facteurs
  structurés : des listes nommées ne sont pas des niveaux numériques ordonnés de
  trajectoire. Utilisez Grid pour une démonstration, ou Sobol multi-scramble avec
  gouvernance complète si son modèle probabiliste est défendable.
- Les clés sont exactement `configurations` en démonstration, ou
  `configurations` et `distribution` en campagne scientifique.
- Chaque charge est une liste ordonnée non vide de chaînes non vides uniques.
  Les labels sont non vides et les charges doivent être distinctes; les alias
  sont refusés.
- La valeur de base dans votre configuration eqasim doit déjà être une liste de
  chaînes. eqasim-sa ne crée ni ne convertit cette clé.
- Les métadonnées du schéma v3 sont scellées et canoniques. Ne modifiez jamais à
  la main labels, lignes, `structuredFactors`, versions, `runId` ou marqueurs;
  toute métadonnée absente, réordonnée, aliasée ou non canonique est refusée.
- Tout changement de design, charge, code d'eqasim, manifeste, lockfile ou
  version impose un nouveau nom de campagne (donc un nouveau dossier). Conservez les anciens plans
  comme provenance immuable au lieu de les réparer sur place.

Voir [Dépannage](troubleshooting.md#erreurs-de-facteur-structuré-nommé) pour les
familles d'erreurs correspondantes.
