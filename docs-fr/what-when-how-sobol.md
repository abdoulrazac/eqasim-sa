# Sobol historique : quoi, quand et comment

**Utilisez l'ancien `sobol` uniquement comme démonstration du workflow avec un seul scramble.**

[English](../docs/what-when-how-sobol.md) | **Français**

## Quoi

La méthode historique projette une séquence de Sobol randomisée sur les valeurs
déclarées des facteurs. Cette séquence répartit les points du design de façon
régulière. La méthode utilise une seule randomisation, nommée scramble, puis
rééchantillonne les lignes de ce scramble par bootstrap.

Elle répond à la question : « La chaîne historique de planification et d'analyse
Sobol fonctionne-t-elle ? » Le fichier généré `03-sobol.yml` est
seulement une démonstration.

Une seed est un entier qui sélectionne un flux aléatoire du simulateur. Ici,
c'est un facteur et non une réplication.

L'indice de premier ordre décrit la variation de sortie liée à un facteur seul.
L'indice d'ordre total inclut ce facteur et toutes ses interactions. Une
interaction est un effet combiné de plusieurs facteurs.

## Quand

Utilisez-la pour exécuter la démonstration générée ou lire une provenance
historique. La provenance est le dossier qui décrit un ancien run.

Ne l'utilisez pas pour la priorisation scientifique des facteurs (FP) ni pour
leur fixation (FF). FP demande quel facteur apprendre plus précisément en
premier. FF demande quel facteur peut être fixé dans une tolérance pratique
déclarée, interactions incluses. Un scramble et son bootstrap de lignes ne
fournissent pas la preuve d'incertitude requise.

## Comment

Le YAML suivant est un **fragment**. Il ne s'exécute pas seul.

```yaml
charter:
  status: demonstration
analysis:
  completeness_threshold: 1.0
  bootstrap_resamples: 99
  confidence_level: 0.95
experiment:
  method: sobol
  seed_role: factor
  base_sample_size: 4
  design_seed: 20260814
parameters:
  seed:
    values: [1401, 1402]
```

Partez du fichier généré complet [`03-sobol.yml`](../src/eqasim_sa/templates/03-sobol.yml).
Le projet généré définit `SA` comme un tableau de commande Bash. `"${SA[@]}"`
lance cette commande sans perdre ses arguments. Voir le [guide de démarrage](getting-started.md#runner-du-projet-généré).

Exécutez d'abord la calibration de ressources générée, comme expliqué dans le
[guide de démarrage](getting-started.md#première-calibration-des-ressources).
`check` et `estimate` ont besoin de ses observations de durée et de stockage.
Utilisez ensuite ces commandes. `plan` crée et scelle le plan. `check` valide le
plan et l'exécution. `estimate` donne le coût attendu en ressources. `run` exécute les runs
manquants. `analyze` crée les résultats.

```bash
TARGET=configs/03-sobol.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## Ce que vous obtenez

- `indicators.parquet` : une ligne par run terminé et par indicateur.
- `sobol-indices.parquet` : les estimations historiques de premier ordre et d'ordre total, avec intervalles par bootstrap de lignes.
- `output-summary.parquet` : un résumé de chaque indicateur sur les positions du design.
- `governed-report.json` : un rapport avec `inferenceStatus: legacy_noninferential` et `claimAuthorized: false`.
- `analysis-generation.json` : les chemins et les empreintes de cette analyse.

Lisez les indices uniquement comme une sortie de démonstration historique. Ne
traitez pas leurs intervalles comme une preuve issue de scrambles indépendants.

Une seule séquence randomisée ne montre pas comment les estimations varient entre des
séquences randomisées indépendamment. Rééchantillonner les lignes de cette même
séquence, par bootstrap de lignes, ne remplace pas ces randomisations.

## Ce que vous pouvez dire

Vous pouvez dire que la démonstration a produit des estimations Sobol historiques
et validé la chaîne d'analyse.

Vous ne pouvez pas faire de revendication scientifique FP ou FF. Vous ne pouvez
pas dire que le bootstrap des lignes d'un scramble remplace plusieurs scrambles
indépendants.

## Checklist avant exécution

- Vérifiez que la campagne est une démonstration ou une provenance historique.
- Vérifiez que `seed` est un facteur entier déclaré.
- Vérifiez que `base_sample_size` est une puissance de deux d'au moins 2.
- Vérifiez les positions prévues et la réutilisation due à la projection discrète.
- Exécutez d'abord la calibration de ressources référencée.
- Utilisez Sobol multi-scramble pour une nouvelle campagne scientifique FP ou FF.

## Suite

- Précédent : [Grid](what-when-how-grid.md)
- Suivant : [Sobol multi-scramble](what-when-how-multiscramble_sobol.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
