# Morris canonique : quoi, quand et comment

**Utilisez Morris canonique uniquement pour un criblage scientifique qui conserve des facteurs pour une étude ultérieure.**

[English](../docs/what-when-how-canonical_morris.md) | **Français**

## Quoi

Le criblage identifie les facteurs qui doivent rester à l'étude. Il ne montre pas
qu'un facteur peut être fixé.

Morris canonique utilise des chemins ordonnés dans un espace de probabilité
unitaire. Cet espace est une échelle commune de 0 à 1. Un facteur change à chaque
pas. Tous les facteurs utilisent le même pas, nommé `Delta`, avant la conversion
vers les valeurs déclarées. `levels` est le nombre pair de
positions autorisées sur l'échelle. Un pas commun signifie que chaque facteur
avance du même `Delta`.

L'implémentation actuelle accepte seulement une distribution uniforme discrète
avec une transformation identité. En termes simples, chaque valeur distincte
déclarée a la même probabilité et la méthode ne remodèle pas ces valeurs.

La méthode répond à la question : « Quels facteurs doivent rester dans l'étude
suivante ? » Elle cible la moyenne conditionnelle, c'est-à-dire l'indicateur
moyen sur les seeds déclarées du simulateur. Une seed est un entier qui
sélectionne un flux aléatoire du simulateur. Cette cible est l'estimand, la
quantité que l'étude veut estimer.

## Quand

Utilisez-la seulement avec une charte scientifique et des facteurs indépendants.
L'indépendance signifie que connaître un facteur n'informe pas sur un autre.
Déclarez un nombre pair de niveaux, un pas commun et un budget. Un checkpoint est
une taille planifiée où la stabilité est évaluée. Un critère d'arrêt est une
règle déclarée à l'avance pour juger la stabilité suffisante. Un contrôle de
robustesse teste la conclusion sous une autre hypothèse déclarée.
Commencez par un pilote séparé. Utilisez sa recommandation de réplication dans
une nouvelle campagne de production et une nouvelle racine.

Ne l'utilisez pas pour la priorisation des facteurs (FP), la fixation des
facteurs (FF), les parts de variance, les configurations structurées nommées ou
les facteurs dépendants. Une part de variance est la portion de variation de la
sortie attribuée à une entrée. Ne présentez pas un pilote comme une production.

## Comment

Le YAML suivant est un **fragment**. Il omet des données obligatoires sur l'étude
et l'exécution. Il ne s'exécute pas seul.

```yaml
charter:
  status: scientific
  decision:
    objective: screening
  uncertainty:
    dependence:
      kind: independent
  stochastic:
    target: conditional_mean
    common_random_numbers: within_trajectory
    allocation:
      phase: pilot
experiment:
  method: canonical_morris
  seed_role: replication
  levels: 4
  trajectories: 8
  candidate_trajectories: 32
  design_seed: 20260820
  replications:
    seeds: [1301, 1302]
```

Les sources Nantes exigent `render_protocol.py`. Ce ne sont pas des modèles
génériques. Suivez les [instructions exactes de rendu Nantes](../protocols/nantes-scientific/README.md#rendering).
Le rendu du pilote exige tous les chemins machine et une nouvelle racine absolue :

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/01-canonical-morris-pilot.yml rendered/pilot.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/scratch" --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/canonical-morris-pilot"
```

Le projet définit `SA` comme un tableau de commande Bash. `"${SA[@]}"` lance
cette commande sans perdre ses arguments. Voir le [guide de démarrage](getting-started.md#runner-du-projet-généré).
`plan` crée et scelle le plan. `check` valide le plan et l'exécution. `estimate`
donne le coût attendu en ressources. `run` exécute les runs manquants. `analyze` crée les
résultats. Les fichiers scientifiques Nantes n'ont pas de bloc de calibration.
Lancez donc d'abord `plan`. Obtenez ensuite une estimation externe ou de
calibration valide, ou exécutez explicitement selon la procédure de votre site.
Un nouvel appel à `check` ou `estimate` n'est pas garanti sans observations.

Exécutez et analysez le pilote :

```bash
PILOT=rendered/pilot.yml
"${SA[@]}" plan --meta-config "$PILOT"
"${SA[@]}" run --meta-config "$PILOT"
"${SA[@]}" analyze --meta-config "$PILOT"
```

Inspectez `replication-allocation.json`. Continuez seulement si son statut est
`recommended`. Avec `capped` ou `infeasible`, arrêtez et révisez l'étude. Ne
continuez jamais automatiquement. Le renderer de production lit le pilote avec
`--pilot-campaign-root` :

```bash
python protocols/nantes-scientific/render_protocol.py \
  protocols/nantes-scientific/02-canonical-morris-production.yml rendered/production.yml \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/study/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/scratch" --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/campaigns/canonical-morris-production" \
  --pilot-campaign-root "$ROOT/campaigns/canonical-morris-pilot"
```

Exécutez et analysez la production :

```bash
PRODUCTION=rendered/production.yml
"${SA[@]}" plan --meta-config "$PRODUCTION"
"${SA[@]}" run --meta-config "$PRODUCTION"
"${SA[@]}" analyze --meta-config "$PRODUCTION"
```

Pour une autre étude, rédigez une configuration complète. Il n'existe pas
d'étape de rendu générique pour une étude quelconque.

## Ce que vous obtenez

- `indicators.parquet` : une ligne par run terminé et par indicateur.
- `replication-dispersion.parquet` : la variation entre les seeds du simulateur.
- `canonical-morris-ranking.parquet` : les rangs finaux et leurs intervalles. Un intervalle est une plage qui montre l'incertitude du design.
- `canonical-morris-convergence.parquet` : les mêmes éléments aux checkpoints emboîtés.
- `canonical-morris-rank-reversals.parquet` : les inversions de rang. Une inversion arrive quand le rééchantillonnage échange l'ordre de deux facteurs.
- `noise-floor.parquet` : la variation entre seeds et l'erreur Monte Carlo. Cette erreur vient du nombre fini de seeds simulées.
- `replication-allocation.json` : la recommandation du pilote pour une nouvelle production.
- `output-summary.parquet`, `convergence-evidence.parquet`, `governed-report.json` et `analysis-generation.json` : résumés, arrêt, statut gouverné, chemins et empreintes.

Le bootstrap rééchantillonne plusieurs fois des trajectoires entières pour
mesurer l'incertitude du design fini. Il ne traite pas chaque réplication de seed
comme une trajectoire.

Lisez les statuts ainsi :

- `pilot_only` : utilisez le résultat seulement pour planifier les réplications.
- `unstable` : ne tirez aucune conclusion de criblage.
- `conditional` : formulez seulement un résultat conditionnel interne.
- Robustesse en attente ou `claimAuthorized: false` : ce n'est pas une décision finale autorisée.

## Ce que vous pouvez dire

Vous pouvez dire que le criblage de production conserve conditionnellement des
facteurs si les critères d'arrêt déclarés passent. Indiquez la charte et les
contrôles de robustesse en attente.

Vous ne pouvez pas dire que le criblage prouve qu'un facteur est peu important,
estime une part de variance, donne un résultat FP ou FF, ou autorise une
revendication. Un pilote sert seulement à planifier les réplications.

Des intervalles larges, des inversions de rang ou une non-convergence doivent
rester visibles. Créez une nouvelle campagne avec un budget prédéclaré plus
grand, ou conservez les facteurs concernés. Ne cachez jamais cette instabilité.

## Checklist avant exécution

- Déclarez le cadre de décision, le modèle d'incertitude, l'estimand et l'indicateur principal.
- Justifiez l'indépendance des facteurs et chaque distribution discrète.
- Utilisez un nombre `levels` pair d'au moins 4 et au moins deux trajectoires.
- Vérifiez les checkpoints, le bootstrap, le budget et les critères d'arrêt.
- Séparez les noms et les racines du pilote et de la production.
- Déclarez les contrôles de robustesse avant `plan`.

## Suite

- Précédent : [OAT discret](what-when-how-discrete_oat.md)
- Suivant : [Sobol multi-scramble](what-when-how-multiscramble_sobol.md)
- Références : [YAML](yaml-reference.md), [paramètres et méthodes](parameters-and-methods.md) et [workflow et résultats](workflow-and-results.md)
