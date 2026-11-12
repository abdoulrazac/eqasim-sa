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

## Le contrôle `path_length` échoue sous Windows

Cause : `check` estime le chemin le plus long qu'eqasim créera comme la longueur
de `execution.scratch`, plus le préfixe worker de cette campagne
(`esa/<campagne>/w<slot>/a<12 hex>/`, avec le nom réel de la campagne et un slot
de 4 chiffres, ou la disposition de `check --smoke`
`.smoke-<8 hex>/s/esa/smoke/w<slot>/a<12 hex>/` si elle est plus longue), plus un budget de 240 caractères pour les chemins relatifs propres
à eqasim. Ce budget a été mesuré sur un vrai run Nantes : le cache d'eqasim crée
des chemins relatifs allant jusqu'à 231 caractères (archive IGN IRIS extraite dans
`data.spatial.iris`).

Avec ce budget, le préfixe worker et le cache d'eqasim dépassent déjà à eux seuls
260 caractères : **aucun `execution.scratch` ne peut convenir** sous Windows sans
chemins longs, pas même une racine de lecteur comme `C:\`. L'erreur indique alors
`estimated path length N exceeds the Windows limit of 260 characters; no
execution.scratch can fit...`. `check` échoue tôt, avant tout run long, plutôt
qu'en plein pipeline.

Remède : c'est le seul cas qui exige un administrateur. Soit un administrateur
active une fois les chemins longs Windows (`LongPathsEnabled`), en dehors
d'eqasim-sa, soit une modification amont d'eqasim (extraire l'archive IRIS dans un
dossier plus court) supprime ce besoin. `check` lit cette valeur du registre en
lecture seule et passe lorsqu'elle est activée ; eqasim-sa ne la modifie jamais. Si
un futur eqasim demande un budget plus petit, `check` indique au contraire la
longueur maximale d'`execution.scratch` qui convient (`choose an execution.scratch
of at most N characters`) ; placez alors le scratch sur un chemin court comme
`C:\esa`. Sous Linux et macOS, les limites sont de 4096 et 1024 caractères et
`check` ne fait qu'avertir.

## Nom de campagne ou d'artefact refusé

Les noms de campagne sont limités à 64 caractères. Sur tous les systèmes,
eqasim-sa refuse les noms réservés Windows (`CON`, `PRN`, `AUX`, `NUL`,
`COM1`-`COM9`, `LPT1`-`LPT9`, avec ou sans extension, quelle que soit la casse),
les noms terminés par un point ou une espace, et les noms qui ne diffèrent que par
la casse. Une campagne créée sous Linux reste ainsi transférable vers Windows et
macOS. Renommez la campagne, l'artefact ou l'indicateur.

## Fins de ligne et `.gitattributes`

Un checkout Git sous Windows peut convertir LF en CRLF. eqasim-sa hache les
entrées texte après normalisation CRLF vers LF : un checkout CRLF produit donc les
mêmes identités que sur le cluster. Cela concerne le hash de `uv.lock`
(`lockfileSha256`), la forme JSON canonique du manifeste de données
(`dataManifestSha256`), l'identité logicielle d'`eqasim-sa`, le texte des
indicateurs intégrés et des plugins (les fichiers binaires restent bruts) et le
contrôle Git de `model.config`, qui utilise `git hash-object --path` pour appliquer
les règles de conversion de Git. Les fichiers de données restent hachés octet pour
octet. `init` écrit aussi un `.gitattributes` (sans jamais écraser le vôtre) qui
impose LF pour les fichiers YAML, JSON et `*.lock`. Si `check` signale une
divergence après un transfert, comparez les fichiers avec `git status` et
`git diff` avant de soupçonner les données.

## Backend SLURM sous Windows ou macOS

Le backend `slurm` exige Linux. `check` signale l'élément `backend` en échec avec
`the slurm backend requires Linux; run eqasim-sa on the cluster`, et `run` refuse
avec le même message. Préparez l'étude sur le PC, copiez-la vers le cluster par
Git, `scp` ou `sftp`, refaites le rendu des chemins machine (marqueurs
`__MACHINE_*__` de `protocols/*/` avec `render_protocol.py`, ou édition des chemins
du YAML), puis lancez `check`, `plan` et `run` sur le cluster. Le PC ne soumet
jamais de job.

## `campaign runs were executed on another machine`

Message complet : `campaign runs were executed on another machine or with other
inputs: <causes> (differing: ...); prepare a new campaign on this machine`.
Cause : des runs terminés de la campagne portent une preuve d'exécution (chemin et
hash de l'exécutable, environnement enfant, invocation, configuration du modèle,
identité d'`eqasim-sa`) différente de celle de cette machine. Une campagne
s'exécute entièrement sur une seule machine : `run` refuse donc de la reprendre et
`check` signale l'élément `same_machine` en échec. Le message nomme chaque cause en
clair, suivie des champs `differing` :

| Cause | Champs | Raison typique |
| --- | --- | --- |
| `executable differs` | `executable_sha256`, `invocation` | autre machine, ou environnement eqasim réinstallé ou déplacé |
| `environment differs` | `environment_sha256`, `environment_variables` | autre machine, ou environnement de shell différent (environnement virtuel, `PATH`, autres variables) |
| `model configuration changed` | `model_config_sha256` | le fichier `model.config` d'eqasim a été modifié depuis les premiers runs |
| `eqasim-sa installation changed` | `eqasim_sa_sha256` | `eqasim-sa` a été mis à jour ou réinstallé |

Remède : préparer une nouvelle campagne (nouveau `campaign_root`) sur cette
machine, ou rétablir ce qui a changé. Les variables propres à la session
(identifiants de terminal et de session SSH comme `WT_SESSION`,
`TERM_SESSION_ID`, `SSH_CONNECTION`, `DISPLAY`, `SHLVL`, `PWD`) sont ignorées :
ouvrir un nouveau terminal ne déclenche pas le refus ; les autres variables entrent
dans le hash d'environnement, reprenez donc depuis un environnement semblable à
celui qui a démarré la campagne. Pour le backend `slurm`, `same_machine` est ignoré sur la machine qui
soumet et appliqué dans le job Slurm.

## Analyser sur une autre machine

`analyze` n'a besoin ni de l'arborescence de données ni du scratch, et ne compare
jamais la preuve d'exécution à la machine qui analyse. Il faut : le dossier de
campagne rapatrié (`scp` ou `sftp`), le dépôt eqasim au même commit, le même
`uv.lock` et le même manifeste de données, et le rendu des chemins machine refait
pour cette machine. `metaConfigSha256` est une provenance publiée, pas une
contrainte d'égalité : des chemins machine différents ne bloquent pas l'analyse.

## Ctrl+C sous Windows

Le processus enfant eqasim est démarré dans son propre groupe de processus, à
l'intérieur d'un Job Object Windows. Lors d'un Ctrl+C, la gestion d'interruption
d'eqasim-sa termine tout l'arbre de processus : aucun descendant ne garde de
fichiers ouverts dans le scratch. Le run est enregistré comme interrompu et peut
être repris avec `run`.

## Système de fichiers non pris en charge

Gardez `campaign_root`, `execution.scratch` et les données sur un disque local
NTFS, APFS, ext4 ou xfs, ou sur le système de fichiers partagé du cluster. Les
volumes FAT/exFAT et les partages SMB ne sont pas pris en charge pour les
campagnes (verrous et publication atomique non garantis).

## Avertissements de sécurité

- Ne modifiez jamais `plan.parquet`.
- Ne créez, ne copiez et ne modifiez jamais `runs/<runId>.parquet`.
- Ne supprimez pas locks, scratch ou campagne pendant un processus ou job actif.
- Ne réutilisez pas une racine après changement de modèle, données, lockfile ou version.
- Ne considérez jamais une archive seule comme un run complet.

Pour les définitions de marqueur, archive, scratch et override, voir le
[glossaire](glossary.md).
