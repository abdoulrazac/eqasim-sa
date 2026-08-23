# Sobol multi-scramble : quoi, quand et comment

**Utilisez Sobol multi-scramble uniquement pour la priorisation scientifique ou la fixation des facteurs.**

[English](../docs/what-when-how-multiscramble_sobol.md) | **Français**

## Quoi

La priorisation des facteurs (FP) demande quel facteur doit être appris plus
précisément en premier pour réduire l'incertitude attendue de la sortie. La
fixation des facteurs (FF) demande si un facteur peut être fixé dans une tolérance
pratique déclarée, avec toutes ses interactions. Une interaction est un effet
combiné de plusieurs facteurs.

La méthode exécute plusieurs séquences de Sobol randomisées indépendamment.
Chaque randomisation est un scramble indépendant. Les tailles d'échantillon
emboîtées réutilisent des préfixes exacts dans chaque scramble. Un checkpoint est
l'une de ces tailles planifiées. La preuve compare les scrambles. Elle ne
rééchantillonne pas les lignes d'un seul scramble. Un bootstrap rééchantillonne
plusieurs fois les unités observées pour mesurer l'incertitude.

La seed finie du simulateur est un facteur de nuisance non actionnable. Elle
reste diagnostique, mais ne reçoit ni rang FP ni décision FF.
Une seed est un entier qui sélectionne un flux aléatoire du simulateur.

## Quand

Utilisez la méthode seulement avec une charte scientifique, un objectif `fp` ou
`ff`, des facteurs indépendants, au moins quatre seeds de scrambles indépendants,
des tailles emboîtées, un budget, des critères d'arrêt et des contrôles de
robustesse. L'indépendance signifie que connaître un facteur n'informe pas sur un
autre. Un critère d'arrêt est une règle déclarée à l'avance pour juger la
stabilité suffisante. Un contrôle de robustesse teste le résultat sous une autre
hypothèse déclarée.

Avant de l'utiliser, écrivez pourquoi les facteurs sont indépendants. Pour chaque
facteur discret, écrivez pourquoi chaque étiquette a la même probabilité.
Déclarez au moins quatre seeds de scramble distinctes. Déclarez des tailles qui
doublent, un budget, un seuil d'arrêt et des contrôles de robustesse. Si vous ne
pouvez pas justifier tous ces choix, n'utilisez pas cette méthode.

Ne l'utilisez pas pour le criblage ou pour des facteurs dépendants. Les
configurations structurées nommées sont acceptées seulement si des probabilités
discrètes égales et leur provenance sont scientifiquement défendables. La
provenance est la trace écrite qui justifie ces probabilités.

## Comment

Le YAML suivant est un **fragment**. Il omet des données obligatoires sur la
charte et l'exécution. Il ne s'exécute pas seul.

```yaml
charter:
  status: scientific
  decision:
    objective: fp
  uncertainty:
    dependence:
      kind: independent
experiment:
  method: multiscramble_sobol
  seed_role: factor
  base_sample_sizes: [8, 16, 32]
  scramble_seeds: [44101, 44102, 44103, 44104]
parameters:
  seed:
    values: [44011, 44012, 44013, 44014]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Support fini de nuisance déclaré
```

Les sources Nantes exigent `render_protocol.py`. Suivez les
[instructions exactes de rendu Nantes](../protocols/nantes-scientific/README.md#rendering).
La porte accepte seulement une production Morris canonique complète, convergée
et conditionnelle. Fournissez cette campagne avec `--screening-campaign-root` :

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/03-multiscramble-fp.yml rendered/fp.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/scratch" --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/multiscramble-fp" \
  --screening-campaign-root "$ROOT/campaigns/canonical-morris-production"
```

Pour FF, utilisez `04-multiscramble-ff.yml`, un autre fichier de sortie et une
autre racine. Pour une autre étude, rédigez une configuration complète. Il
n'existe pas de rendu automatique pour une étude générique.

Le projet définit `SA` comme un tableau de commande Bash. `"${SA[@]}"` lance
cette commande sans perdre ses arguments. Voir le [guide de démarrage](getting-started.md#runner-du-projet-généré).
`plan` crée et scelle le plan. `check` valide le plan et l'exécution. `estimate`
donne le coût attendu en ressources. `run` exécute les runs manquants. `analyze` crée les
résultats. Les fichiers scientifiques Nantes n'ont pas de bloc de calibration.
Lancez donc d'abord `plan`. Obtenez ensuite une estimation externe ou de
calibration valide, ou exécutez explicitement selon la procédure de votre site.
Un nouvel appel à `check` ou `estimate` n'est pas garanti sans observations.

```bash
TARGET=rendered/fp.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

## Ce que vous obtenez

- `indicators.parquet` : une ligne par run terminé et par indicateur.
- `multiscramble-sobol-indices.parquet` : les indices agrégés finaux et les rangs de l'objectif.
- `multiscramble-sobol-by-scramble.parquet` : les estimations indépendantes par scramble et checkpoint.
- `multiscramble-sobol-convergence.parquet` : les intervalles entre scrambles et les changements entre checkpoints. Cet intervalle est une plage calculée avec les séquences randomisées indépendamment.
- `multiscramble-sobol-diagnostics.parquet` : les diagnostics de plage, de contraste et de réutilisation du design.
- `output-summary.parquet`, `convergence-evidence.parquet`, `governed-report.json` et `analysis-generation.json` : résumés, arrêt, statut gouverné, chemins et empreintes.

La moyenne de premier ordre est la part moyenne estimée de variation de sortie
liée à un facteur seul. En FP, une part plus grande signifie que ce facteur doit
être appris plus précisément en premier. Pour FF, la méthode utilise l'extrémité
haute de son intervalle d'incertitude d'ordre total. L'ordre total inclut toutes
les interactions. Un facteur est candidat à la fixation seulement si cette
extrémité ne dépasse pas la tolérance de l'indicateur principal. Cette extrémité
est une preuve d'incertitude, pas un maximum garanti.

Le statut reste `unstable` jusqu'au passage de tous les critères d'arrêt, puis
devient `conditional`. Un résultat conditionnel peut être rapporté comme
provisoire dans l'étude déclarée. Ce n'est pas une décision finale robuste et
autorisée tant que des contrôles sont en attente et que `claimAuthorized` vaut
`false`.

## Ce que vous pouvez dire

Pour FP, vous pouvez donner une priorité d'apprentissage conditionnelle après le
passage des critères d'arrêt. Pour FF, vous pouvez dire qu'un facteur actionnable
est conditionnellement dans la tolérance déclarée, interactions incluses, pour
l'indicateur principal scellé.

Vous ne pouvez pas faire une revendication autorisée ou robuste tant que la
robustesse est en attente. Vous ne pouvez ni classer ni fixer la seed de nuisance.
Vous ne pouvez pas étendre le résultat à des supports, dépendances, indicateurs
ou scénarios non déclarés.

## Checklist avant exécution

- Déclarez FP ou FF, le cadre, l'estimand et l'indicateur principal. L'estimand est la quantité que l'étude veut estimer.
- Pour FF, déclarez la tolérance pratique avant `plan`.
- Justifiez l'indépendance et chaque distribution discrète.
- Utilisez au moins quatre seeds de scramble uniques et des tailles qui doublent.
- Vérifiez le budget, les critères d'arrêt et les contrôles de robustesse en attente.
- Gardez la seed finie du simulateur non actionnable.

## Suite

- Précédent : [Morris canonique](what-when-how-canonical_morris.md) et [Sobol historique](what-when-how-sobol.md)
- Suivant : [Workflow et résultats](workflow-and-results.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
