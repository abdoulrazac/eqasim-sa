# Grid : quoi, quand et comment

**Utilisez Grid pour une démonstration lisible qui compare toutes les combinaisons déclarées.**

[English](../docs/what-when-how-grid.md) | **Français**

## Quoi

Grid forme toutes les combinaisons des valeurs de facteurs et des seeds de
réplication. Un facteur est une entrée que la campagne fait varier. Grid répond à
la question : « Quelle sortie est observée pour chaque combinaison déclarée ? »

Une seed est un entier qui sélectionne un flux aléatoire du simulateur. Les seeds
de réplication répètent une combinaison pour montrer la variation aléatoire entre
seeds.

Le design est facile à inspecter. Grid accepte aussi des configurations
structurées nommées. Ces configurations donnent des noms courts à des listes
ordonnées, par exemple à plusieurs listes `matching_attributes`.

## Quand

Utilisez Grid pour valider la chaîne de configuration, d'exécution, de stockage
et d'analyse. Utilisez-le pour une petite comparaison ou comme premier
design de configurations structurées nommées.

Ne l'utilisez pas pour estimer des parts de variance ou faire des revendications
inférentielles. Une part de variance est la portion de variation de la sortie
attribuée à une entrée. Une revendication inférentielle est une conclusion qui
dépasse les combinaisons observées. Ne l'utilisez pas si le produit des tailles de domaines et du
nombre de seeds est trop grand. Ce produit augmente vite.

## Comment

Le YAML suivant est un **fragment**. Il ne s'exécute pas seul.

```yaml
charter:
  status: demonstration
experiment:
  method: grid
  seed_role: replication
  replications:
    seeds: [1201, 1202]
parameters:
  sampling_rate:
    values: [0.001, 0.002]
```

Partez du fichier généré complet [`01-grid.yml`](../src/eqasim_sa/templates/01-grid.yml).
Pour les facteurs sous forme de listes ordonnées, suivez le [tutoriel complet des configurations structurées](structured-configurations-tutorial.md).
Le projet généré définit `SA` comme un tableau de commande Bash. `"${SA[@]}"`
lance cette commande sans perdre ses arguments. Voir le [guide de démarrage](getting-started.md#runner-du-projet-généré).

Exécutez d'abord la calibration de ressources générée, comme expliqué dans le
[guide de démarrage](getting-started.md#première-calibration-des-ressources).
`check` et `estimate` ont besoin de ses observations de durée et de stockage.
Utilisez ensuite ces commandes. `plan` crée et scelle le plan. `check` valide le
plan et l'exécution. `estimate` donne le coût attendu en ressources. `run` exécute les runs
manquants. `analyze` crée les résultats.

```bash
TARGET=configs/01-grid.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## Ce que vous obtenez

- `indicators.parquet` : une ligne par run terminé et par indicateur.
- `replication-dispersion.parquet` : la variation entre les seeds de réplication.
- `output-summary.parquet` : un résumé de chaque indicateur sur les positions du design.
- `governed-report.json` : un rapport avec `inferenceStatus: demonstration_noninferential` et `claimAuthorized: false`.
- `analysis-generation.json` : les chemins et les empreintes de cette analyse.

Lisez les résultats comme une comparaison directe des combinaisons déclarées.
Une grande différence désigne une combinaison à examiner. Ce n'est pas une part
de variance.

## Ce que vous pouvez dire

Vous pouvez dire que la démonstration a comparé toutes les combinaisons déclarées
et que certaines ont produit des sorties observées différentes.

Vous ne pouvez pas dire que Grid prouve une importance globale, permet la
priorisation des facteurs, permet la fixation des facteurs ou fournit une preuve
inférentielle.

## Checklist avant exécution

- Vérifiez que la campagne est une démonstration.
- Vérifiez que chaque clé de facteur existe dans la configuration active du modèle.
- Vérifiez que chaque valeur scalaire candidate a un type valide et peut être transmise au modèle.
- Pour un facteur structuré, vérifiez que sa clé de base est une liste.
- Calculez le nombre total de combinaisons avant `plan`.
- Utilisez des valeurs et des seeds uniques.
- Exécutez d'abord la calibration de ressources référencée.
- Utilisez un nouveau `campaign_root` après un changement de design ou de contexte.

## Suite

- Précédent : [Démarrage](getting-started.md)
- Suivant : [OAT discret](what-when-how-discrete_oat.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
