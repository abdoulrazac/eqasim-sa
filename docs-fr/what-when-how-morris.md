# Morris historique : quoi, quand et comment

**Utilisez l'ancien `morris` uniquement pour lire ou reproduire une provenance de démonstration historique.**

**Règle pour débuter : utilisez-le seulement si un YAML ou un plan existant
contient déjà `method: morris`. Ne créez jamais une nouvelle campagne `morris`.**

[English](../docs/what-when-how-morris.md) | **Français**

## Quoi

L'ancien `morris` est le plan historique à niveaux adjacents. Il change un
facteur numérique à la fois entre deux valeurs voisines déclarées. Ses effets
normalisés locaux sont non canoniques : ils ne correspondent pas à la définition
Morris actuelle.

Une seed est un entier qui sélectionne un flux aléatoire du simulateur. Cette
méthode utilise les seeds comme réplications du même réglage des facteurs.

Cette méthode répond seulement à la question : « Que contient cette ancienne
campagne ? » Elle existe pour garder les anciens plans et leur provenance
lisibles. La provenance est le dossier qui décrit un ancien run.

## Quand

Utilisez-la si un plan ou un enregistrement existant contient déjà
`method: morris` et si vous devez examiner son comportement historique.

Ne l'utilisez jamais pour une nouvelle campagne. Utilisez [OAT discret](what-when-how-discrete_oat.md)
pour une nouvelle démonstration à niveaux adjacents. Utilisez [Morris canonique](what-when-how-canonical_morris.md)
pour un criblage scientifique.

## Exemples de paramètres

Ne créez jamais un nouveau fichier à partir de cet exemple. Il sert seulement à
lire des paramètres susceptibles d'apparaître dans un ancien YAML :

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
  matching_minimum_observations:
    values: [5, 20, 50]
```

Les deux domaines sont numériques et strictement croissants, comme l'exige cet
ancien parseur. Un changement adjacent tel que `0.001` vers `0.002`, ou `20`
vers `50`, s'interprète seulement dans ce design historique. Ce n'est pas une
recommandation actuelle de facteurs ou de valeurs.

Chaque clé de facteur doit déjà exister sous `config:` dans le YAML modèle
scellé. Un facteur structuré exigerait aussi une clé de base de type liste, mais
cette méthode refuse les facteurs structurés. Les domaines d'exemple montrent la
syntaxe, pas une justification scientifique automatique. Les seeds appartiennent
uniquement à `experiment.replications.seeds` dans cette méthode historique ;
n'ajoutez jamais `random_seed` comme facteur ordinaire. Consultez le [catalogue
des paramètres Nantes](parameters-and-methods.md#catalogue-des-paramètres-nantes).

## Comment

Le YAML suivant est un **fragment historique**. Il ne s'exécute pas seul et ne
recommande pas une nouvelle campagne.

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

Il n'existe volontairement aucun starter complet `morris`. Consultez le [schéma
YAML historique](yaml-reference.md). Pour une nouvelle démonstration complète,
partez de [`02-discrete-oat.yml`](../src/eqasim_sa/templates/02-discrete-oat.yml).
Si vous devez réanalyser une configuration historique, utilisez son fichier
d'origine. Le projet d'origine définit `SA` comme un tableau de commande Bash.
`"${SA[@]}"` lance cette commande sans perdre ses arguments. Voir le
[guide de démarrage](getting-started.md#runner-du-projet-généré).

`plan` crée et scelle le plan. `check` valide le plan et l'exécution. `estimate`
donne le coût attendu en ressources. `run` exécute les runs manquants. `analyze` crée les
résultats. `check` et `estimate` exigent des observations de ressources. Utilisez
seulement celles que référence la configuration historique. N'inventez pas une
nouvelle calibration pour une ancienne provenance.

```bash
TARGET=configs/existing-morris.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## Ce que vous obtenez

- `indicators.parquet` : une ligne par run terminé et par indicateur.
- `replication-dispersion.parquet` : la variation entre les seeds de réplication.
- `morris-ranking.parquet` : les effets non canoniques à niveaux adjacents, classés par `mu_star`.
- `output-summary.parquet` : un résumé de chaque indicateur sur les positions du design.
- `governed-report.json` : un rapport avec `inferenceStatus: demonstration_noninferential` et `claimAuthorized: false`.
- `analysis-generation.json` : les chemins et les empreintes de cette analyse.

Traitez le classement comme une description historique. Un rang élevé signifie
que des effets locaux plus grands sont apparus dans cet ancien design déclaré.

## Ce que vous pouvez dire

Vous pouvez décrire l'ancien ordre des facteurs et les effets locaux observés.
Précisez que ces effets ne sont pas canoniques.

Vous ne pouvez pas les appeler effets Morris canoniques. Vous ne pouvez pas en
déduire des parts de variance, une importance globale, une priorisation ou une
fixation des facteurs.

## Checklist avant exécution

- Vérifiez qu'il s'agit d'une campagne historique existante.
- Conservez le plan, le contexte, les valeurs et les seeds d'origine.
- Vérifiez que tous les domaines sont numériques et strictement croissants.
- Ne remplacez jamais les octets d'un ancien plan à la main.
- Préférez une nouvelle méthode et un nouveau `campaign_root` pour un nouveau travail.

## Suite

- Précédent : [Grid](what-when-how-grid.md)
- Suivant : [OAT discret](what-when-how-discrete_oat.md) ou [Morris canonique](what-when-how-canonical_morris.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
