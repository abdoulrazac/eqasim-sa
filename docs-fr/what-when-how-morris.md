# Morris historique : relire une ancienne campagne

**Cas pratique : comprendre un dossier existant qui contient déjà `method: morris`.**

[English](../docs/what-when-how-morris.md) | **Français**

## Règle simple

Si vous préparez une nouvelle étude, **n'utilisez pas cette méthode**.

- Pour une petite démonstration numérique, utilisez [OAT discret](what-when-how-discrete_oat.md).
- Pour un criblage scientifique, utilisez [Morris canonique](what-when-how-canonical_morris.md).

L'ancien `morris` reste disponible uniquement pour relire ou reproduire une
campagne historique. Ses effets sont calculés sur des niveaux adjacents avec des
pas locaux normalisés. Ce ne sont pas les effets du Morris canonique actuel.

## Cas pratique : audit d'une campagne archivée

Supposons qu'un collègue vous transmette :

```text
ancienne-etude/
├── configs/morris-2024.yml
└── campaigns/morris-2024/
    ├── plan.parquet
    ├── runs/
    └── archives/
```

Votre question n'est pas « quels paramètres devrais-je étudier aujourd'hui ? »,
mais :

> Que mesurait exactement cette ancienne campagne, et que permettent ses résultats ?

### 1. Vérifier qu'il s'agit bien d'une provenance existante

Le YAML d'origine doit déjà contenir :

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

Le dossier de campagne doit contenir son `plan.parquet` scellé. Le contexte
complet comprend aussi le commit Git du modèle, le manifeste de données, le
lockfile, la version d'`eqasim-sa`, les archives et les marqueurs de runs. Un
simple dossier sans ces éléments ne suffit pas à reproduire l'étude.

Ne recréez pas un plan manquant et ne modifiez pas le plan archivé pour lui faire
accepter un autre contexte. Vous fabriqueriez une nouvelle campagne au lieu de
relire l'ancienne.

### 2. Retrouver la question, les paramètres et les indicateurs

Un ancien YAML peut par exemple contenir :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
  matching_minimum_observations:
    values: [5, 20, 50]

indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
```

Dans cet exemple, la campagne observait les changements locaux de la distance TV
entre la distribution d'âge synthétique et INSEE lorsque `sampling_rate` ou
`matching_minimum_observations` passait d'un niveau voisin au suivant.

Pour un passage d'une valeur `gauche` à une valeur `droite`, l'ancien calcul est :

```text
delta = (droite - gauche) / (maximum du domaine - minimum du domaine)
effet = (indicateur après - indicateur avant) / delta
```

L'effet est donc exprimé pour une variation normalisée du facteur sur l'étendue
déclarée. Il ne s'agit pas d'une dérivée continue ni d'une part de variance.

Les domaines doivent être numériques et strictement croissants. Les seeds sont
des réplications. La méthode historique refuse les facteurs structurés.

Ce bloc décrit ce que l'étude a fait ; il ne justifie pas les valeurs choisies et
ne recommande pas de réutiliser ce design.

### 3. Vérifier le plan sans le remplacer

Restaurez d'abord la **version archivée** d'`eqasim-sa` dans son environnement.
La version courante ne convient pas : la version du runner fait partie du
contexte scellé. Dans l'exemple suivant, `HISTORICAL_ENV` désigne cet
environnement restauré. Le script lit ensuite `campaign_root` dans le YAML pour
vérifier le fichier exact **avant** d'appeler `plan` :

```bash
TARGET=/chemin/vers/ancienne-etude/configs/morris-2024.yml
HISTORICAL_ENV=/chemin/vers/environnement-eqasim-sa-archive
SA=("$HISTORICAL_ENV/bin/eqasim-sa")
HISTORICAL_PYTHON="$HISTORICAL_ENV/bin/python"

PLAN=$("$HISTORICAL_PYTHON" - "$TARGET" <<'PY'
import sys
from pathlib import Path

import yaml

meta = Path(sys.argv[1]).resolve()
root = Path(yaml.safe_load(meta.read_text(encoding="utf-8"))["campaign_root"])
if not root.is_absolute():
    root = (meta.parent / root).resolve()
print(root / "plan.parquet")
PY
)
[[ -f "$PLAN" && ! -L "$PLAN" ]] || {
  printf '%s\n' "Plan historique absent ; arrêter l'audit." >&2
  exit 1
}

RESULT=$("${SA[@]}" plan --meta-config "$TARGET") || exit 1
printf '%s\n' "$RESULT"
STATUS=$(printf '%s' "$RESULT" | "$HISTORICAL_PYTHON" -c \
  'import json, sys; print(json.load(sys.stdin)["status"])')
[[ "$STATUS" == unchanged ]] || {
  printf '%s\n' "Le plan historique n'est pas inchangé ; arrêter l'audit." >&2
  exit 1
}
```

Toute erreur signifie qu'il faut arrêter et retrouver les fichiers d'origine,
pas modifier la provenance.

### 4. Réanalyser seulement les archives complètes

Si tous les runs requis et leurs archives sont présents, vous pouvez reconstruire
les sorties d'analyse :

```bash
"${SA[@]}" analyze --meta-config "$TARGET"
```

`analyze` ne relance pas le simulateur. Si des runs manquent, `run` n'est
acceptable que si l'environnement, l'exécution et le contexte d'origine sont
encore exactement reproductibles. N'ajoutez pas silencieusement de nouvelles
seeds, valeurs ou données.

`check` et `estimate` concernent surtout une nouvelle exécution. Ils exigent des
observations de ressources compatibles ; une calibration inventée aujourd'hui
ne fait pas partie de la provenance historique de 2024.

### 5. Lire le classement historique

Le fichier `morris-ranking.parquet` contient notamment :

| Colonne | Lecture historique |
| --- | --- |
| `mu` | effet local moyen avec son signe |
| `mu_star` | taille moyenne des effets locaux |
| `sigma` | écart-type de tous les effets `T × R` ; il mélange trajectoires et seeds |
| `seed_std` | racine de la variance moyenne entre seeds dans chaque trajectoire |
| `rank` | ordre décroissant de `mu_star` |

Supposons qu'une **ancienne sortie** classe `sampling_rate` avant
`matching_minimum_observations`. Vous pouvez écrire : « Dans le design
historique déclaré, `sampling_rate` présentait les effets locaux normalisés les
plus grands sur l'indicateur âge. »

Un `sigma` élevé ne prouve pas à lui seul une interaction ou une non-linéarité :
la variation entre seeds y contribue aussi. `sigma` et `seed_std` sont tous deux
exprimés en unités d'effet normalisé et peuvent être rapprochés.
`replication-dispersion.parquet` reste utile comme contexte, mais ses écarts-types
sont en unités brutes de l'indicateur TV : ne les comparez pas numériquement aux
deux premières colonnes.

Vous ne devez pas écrire : « Morris prouve que `sampling_rate` est globalement
le facteur le plus important. » Le design n'est pas canonique et n'estime pas de
part de variance.

Si la version archivée produit `governed-report.json`, il doit rester
`demonstration_noninferential` avec `claimAuthorized: false`. Les versions Morris
antérieures à l'introduction du rapport gouverné ne créent pas ce fichier :
documentez son absence dans l'audit et ne le recréez pas avec un runner plus
récent, car cela changerait le contexte scellé.

## Ce que vous pouvez conclure

Vous pouvez décrire les facteurs, niveaux, seeds, indicateurs et effets locaux de
l'ancienne campagne, en indiquant clairement qu'ils sont historiques et non
canoniques.

Vous ne pouvez pas convertir ce classement en criblage scientifique, en
priorisation ou en décision de fixation. Pour une nouvelle question, créez une
nouvelle campagne avec une méthode actuelle et une nouvelle racine.

## Checklist d'audit

- Le YAML et le `plan.parquet` existaient avant l'audit.
- Le modèle, le commit Git, les données, le lockfile et la version sont retrouvés.
- `plan` répond `unchanged`, jamais `created`.
- Les archives requises sont complètes avant `analyze`.
- Aucun plan ni marqueur de run n'est modifié à la main.
- Le rapport gouverné, lorsqu'il existe dans cette version, précise le statut non inférentiel ; sinon l'audit documente son absence.

## Suite

- Nouvelle démonstration : [OAT discret](what-when-how-discrete_oat.md)
- Nouveau criblage scientifique : [Morris canonique](what-when-how-canonical_morris.md)
- Références : [YAML](yaml-reference.md), [compatibilité](compatibility.md) et [workflow et résultats](workflow-and-results.md)
