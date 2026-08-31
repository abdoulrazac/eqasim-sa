# Dépannage

[English](../docs/troubleshooting.md)

## `model Git repository must be clean`

Cause : fichier suivi modifié, fichier non suivi, index différent ou changement
pendant le chargement. Remède : inspecter `git status --short` dans la racine du
modèle, puis versionner ou retirer consciemment les changements. Ne contournez
pas ce contrôle : le commit entre dans `contextHash`.

## `model.config does not match its sealed Git blob`

Cause : le fichier lu n'est pas identique à la version de `HEAD`. Remède :
restaurer ou committer la configuration, puis utiliser une nouvelle campagne si
un plan avait déjà été créé.

## `plan differs; choose a new campaign_root`

Cause : design, paramètres, contexte, version du paquet ou exigences d'artefacts
différents sous une racine déjà scellée. Remède : choisir un nouveau
`campaign_root`. Ne remplacez pas `plan.parquet`.

## `campaign_root contains durable state without a plan`

Cause : état résiduel ou contenu étranger dans la racine. Remède : identifier sa
provenance. Pour une nouvelle étude, choisir une racine vide. Ne fabriquez pas
un plan pour recouvrir un état existant.

## `provide a calibration meta-config with a complete run`

Cause : aucun marqueur complet n'est disponible dans la cible ou les campagnes
de calibration. Remède : `plan`, puis `run` sur la calibration. Lancez ensuite
`plan`, `check` et `estimate` sur la cible.

## `unknown factor` ou `reserved or infrastructure factor`

Cause : nom absent du bloc `config:` eqasim, option dédiée, infrastructure ou
colonne réservée. Remède : utiliser une clé scientifique active; placer seulement
les options d'environnement autorisées dans `execution.overrides`.

## Erreur de type dans `parameters`

Cause fréquente : booléen, `null`, niveaux mixtes, float non fini ou domaine
Morris historique/OAT discret mal ordonné. Remède : conserver un type homogène et des nombres finis.
Pour un float requis, écrire `1.0`, pas `1`.

## Erreurs de facteur structuré nommé

- `factor 'matching_attributes' must contain exactly ...` : une déclaration de
  démonstration contient exactement `configurations`; une déclaration scientifique
  contient exactement `configurations` et `distribution`. Ne mélangez pas
  `values` et `configurations`.
- `unknown factor` ou `model config value must be a list of strings` : le YAML
  modèle scellé doit déjà contenir `config.matching_attributes` active sous forme
  de liste de chaînes. Committez ce YAML et gardez le dépôt modèle propre;
  eqasim-sa ne crée ni ne convertit cette clé.
- `must be a non-empty ordered list...`, `distinct payloads` ou `payload aliases` :
  chaque charge est une liste non vide de chaînes non vides uniques, et deux
  labels ne peuvent résoudre vers la même liste ordonnée. Les labels sont aussi
  des chaînes non vides sans NUL.
- `does not support structured factors` : `morris`, `discrete_oat` et
  `canonical_morris` refusent tous les charges structurées nommées. Utilisez Grid
  pour une démonstration, ou un `multiscramble_sobol` entièrement gouverné si les
  probabilités égales des labels sont scientifiquement justifiées.
- `structured plan metadata ...`, `mismatch` ou `unknown label` : dans le schéma
  v3, `structuredFactorsVersion` et la métadonnée canonique `structuredFactors`
  sont scellés avec les labels et identités. Préservez l'état endommagé pour
  diagnostic et créez un plan sous une nouvelle racine; ne modifiez jamais
  `plan.parquet`.
- L'absence de `--matching_attributes` dans la commande modèle est volontaire.
  Les valeurs composites sont écrites dans le snapshot YAML de chaque run, pas
  transportées par le CLI scalaire de synpp. Inspectez le `config.yml` de la
  tentative en échec au lieu d'ajouter un override CLI manuel contenant la liste.

Le workflow sûr complet figure dans le
[tutoriel des configurations structurées](structured-configurations-tutorial.md).

## `canonical_morris requires charter.status scientific`

Cause : Morris canonique est configuré comme démonstration ou sans charte.
Remède : ne transformez pas mécaniquement le starter OAT discret généré. Rédigez
une charte scientifique, des distributions, un budget, des critères d'arrêt et
des contrôles de robustesse propres à l'étude selon la
[référence YAML](yaml-reference.md#morris-canonique).

## Bloc `stochastic` absent ou incompatible

Toute charte scientifique exige un mode exact. Morris canonique exige
`conditional_mean`, des CRN `within_trajectory` et une allocation; Sobol
multi-scramble exige `finite_seed_factor` sans allocation. Un pilote a sélection
et provenance nulles et au moins son minimum de seeds. La production a une
provenance pilote non vide et exactement son nombre sélectionné. Ne réparez pas
un pilote en ajoutant des seeds ou en changeant sa phase : choisissez une nouvelle
racine de campagne.

## Allocation `capped` ou `infeasible`

`capped` signifie que la formule MCSE demande plus que
`maximum_replications`; la cible n'est donc pas démontrée. `infeasible` signifie
que même les positions structurelles sélectionnées `T*(D+1)*R` dépassent le
budget hors réserve. Révisez précision, plafond, design ou budget dans une
nouvelle campagne; n'abaissez jamais le besoin et ne comptez pas les runs
dédupliqués.

## Erreur d'objectif, de dépendance ou de budget Morris canonique

Morris canonique accepte uniquement l'objectif `screening`, une indépendance des
paramètres et un contrat scientifique complet. Ses positions structurelles du
design, sans déduplication des run IDs, doivent tenir dans
`floor(maximum_runs * (1 - reserve_fraction))` ; la réserve n'est pas disponible
au plan initial. Il impose aussi au plus 500 candidats et un budget
déterministe de calcul des distances. Réduisez le design ou augmentez un budget
scientifiquement justifié ; ne rebaptisez pas FP ou FF en criblage.

## Erreur de checkpoint ou bootstrap Morris canonique

Utilisez au moins 100 rééchantillonnages. Les checkpoints sont des entiers triés,
uniques, au moins égaux à 2 et se terminent par `trajectories` ;
`screening_top_k` ne dépasse pas le nombre de facteurs. Ces diagnostics ne
certifient pas à eux seuls l'arrêt ou la robustesse de la charte.

## `morris` est refusé par une charte scientifique

L'ancien `method: morris` reste lisible uniquement comme provenance de
démonstration. Utilisez `discrete_oat` pour une nouvelle démonstration à niveaux
adjacents, ou rédigez une campagne de criblage scientifique `canonical_morris`
propre à l'étude.

## `duplicate canonical configuration and seed pair`

Cause : plusieurs positions Grid résolvent vers le même couple configuration et
seed, souvent à cause de niveaux dupliqués. Remède : dédupliquer les domaines ou
séparer réellement les configurations.

## `campaign completeness ... is below threshold`

Cause : marqueurs absents ou logs d'échec. Remède : consulter
`logs/<runId>.log`, corriger le pipeline et relancer `run`. Ne créez pas de
marqueur à la main.

## `invalid completion marker`

Cause : fichier manuel, tronqué, mauvais schéma, mauvais contexte, compression
incorrecte ou contenu incohérent. Remède : préserver l'état pour diagnostic,
puis reconstruire le run par le workflow normal dans une racine saine. Une
édition manuelle rend la preuve de complétude non fiable.

## `another invocation is already running this campaign`

Cause : une commande `run` détient déjà le verrou de campagne. Remède : attendre
sa fin. Après un crash, le verrou noyau est libéré automatiquement; ne supprimez
pas les fichiers de verrou pendant une exécution.

## `static check time budget exhausted`

Cause : accès Git, lecture des plans/manifeste, sonde synpp ou mesure disque trop
lente au-delà du budget de 55 secondes. Remède : vérifier les stockages réseau,
la taille des fichiers de contrôle et le temps de démarrage de l'environnement.

## `synpp environment probe failed`

Cause : l'invocation ne démarre pas synpp comme attendu ou ne répond pas avec
`Config file does not exist:` pour le fichier-sonde. Remède : exécuter la liste
`execution.invocation` dans `model.root`, vérifier l'environnement et les
versions. La sonde statique ne lance pas une simulation complète.

## Échec de `check --smoke`

Cause : moins de deux seeds, absence de `sampling_rate`, pipeline réel en échec,
artefact absent ou Parquet invalide. Remède : satisfaire le contrat smoke et
lire le détail retourné. Attention : cette option exécute deux simulations
temporaires à `sampling_rate: 0.001`; elle consomme du calcul et accède aux
données réelles. Le smoke part du YAML modèle scellé et n'applique aucun
`execution.overrides`. Si la campagne normale dépend de
`execution.overrides.data_path`, le smoke utilise malgré tout le `data_path` du
modèle; rendez ce chemin accessible ou testez consciemment depuis un environnement
où il est valide.

## Espace disque ou scratch insuffisant

Cause : `missingDiskBytes` ou `missingScratchBytes` positif. Remède : augmenter
le quota, déplacer `campaign_root` ou `execution.scratch`, ou réduire
`execution.workers`. Le scratch estimé est le maximum observé par worker multiplié
par les workers.

## `meta-config changed while preparing execution`

Cause : le YAML a changé pendant `run`, ou entre soumission et job SLURM.
Remède : arrêter les éditeurs/générateurs concurrents et relancer avec un fichier
stable. Après changement de design ou contexte, choisir une nouvelle racine.

## Variance Sobol constante ou non représentable numériquement

Pour `sobol` historique, toutes les valeurs d'indicateur peuvent être constantes
ou les rééchantillonnages bootstrap peuvent être dégénérés. Vérifiez
l'indicateur, les domaines, `N` et la diversité effective des runs. Ne masquez
pas l'erreur statistique.

Avec `multiscramble_sobol`, une variance non positive ou un contraste A/B
inefficace à un scramble et checkpoint quelconque est fatal. Augmentez la
première taille ou révisez le support scientifiquement justifié ; aucun scramble
n'est supprimé et aucun bootstrap de secours n'est appliqué.

L'analyseur peut aussi signaler `scaling erased base variance`, `output variance
exceeds float64 range` ou `output variance is below float64 range`. Ces erreurs
signifient que des valeurs finies ne permettent pas à la fois le calcul Sobol et
une variance dans les unités carrées d'origine à la précision `float64`.
Inspectez les unités et l'étendue dynamique de l'indicateur, puis ne relancez
qu'après une reformulation ou un changement d'unité scientifiquement justifié.
Ne tronquez jamais les valeurs extrêmes et n'ignorez jamais un scramble en échec
pour obtenir un rapport : la publication est transactionnelle et aucune table
Sobol partielle n'est valide.

Le support fini de seed est toujours une nuisance non actionnable. Un rang ou une
tolérance FF nuls pour `seed` sont intentionnels : ses indices sont diagnostiques,
ni consigne de contrôle ni estimation du bruit intrinsèque sans restriction.

## Avertissements de sécurité

- Ne modifiez jamais `plan.parquet`.
- Ne créez, ne copiez et ne modifiez jamais `runs/<runId>.parquet`.
- Ne supprimez pas locks, scratch ou campagne pendant un processus ou job actif.
- Ne réutilisez pas une racine après changement de modèle, données, lockfile ou version.
- Ne considérez jamais une archive seule comme un run complet.

Pour les définitions de marqueur, archive, scratch et override, voir le
[glossaire](glossary.md).
