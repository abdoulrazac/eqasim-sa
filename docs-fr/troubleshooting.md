# Dépannage

[English](../docs/troubleshooting.md)

## `eqasim-sa requires Python 3.13, but this environment runs Python ...` ou `No module named 'pyarrow.lib'`

L'interpréteur de l'environnement d'outil d'eqasim-sa n'est pas le Python 3.13
pour lequel les paquets ont été installés. Cela arrive quand le lien
`bin/python` d'un environnement d'outil uv pointe vers un autre Python après
l'installation (par exemple après la réinstallation des versions de Python
gérées par uv). À partir de la prochaine version, eqasim-sa le signale avec le
premier message ; la version 0.4.0 échoue dans pyarrow avec le second.
Recréez l'environnement de l'outil avec Python 3.13 :

```bash
uv tool uninstall eqasim-sa
uv tool install --python 3.13 git+https://github.com/abdoulrazac/eqasim-sa@latest
```

Si `eqasim-sa --version` pointe toujours ailleurs, lancez `which -a eqasim-sa`
(ou `where eqasim-sa` sous Windows) : une installation plus ancienne dans un
autre environnement (par exemple un `pip install -e` dans conda) peut passer
avant dans le `PATH` ; désinstallez-la de cet environnement.

## `model.config not found: ... (a relative path is read from model.root)`

Cause : un `model.config` relatif est lu depuis le dossier eqasim
(`model.root`), pas depuis le répertoire de la méta-configuration. Un chemin
comme `../../eqasim-france/config.yml`, écrit relativement à `configs/`, pointe
donc au mauvais endroit. Remède : écrire le chemin relativement à `model.root`
(`config: config.yml`, ou `config: my_config.yml` pour une copie placée à
côté), ou un chemin absolu. Le message affiche la valeur telle qu'écrite dans
la méta-configuration.

## `meta-config not found: ...` et autres fichiers manquants

Toute commande qui lit une méta-configuration nomme le fichier manquant :
`meta-config not found: <chemin>` pour le fichier donné en ligne de commande
(tel que vous l'avez tapé), et `model.root`, `model.data_manifest`,
`model.lockfile` ou `calibration meta-config not found: <valeur> (a relative
path is read from the meta-config folder)` pour un chemin écrit dedans.
Remède : vérifier le chemin depuis le dossier nommé dans le message. Un fichier
qui n'est pas du YAML valide arrête la commande avec `cannot parse meta-config
<chemin>: <erreur>` (ou `cannot parse model.config <valeur>:`, `cannot parse
calibration meta-config <valeur>:`), où `<erreur>` donne la classe d'erreur de
l'analyseur (`ParserError`, `ScannerError`, ...), la ligne, la colonne et la
cause.

## `no eqasim source files found under ...`

Cause : `model.root` ne contient aucun `*.py`, `*.java`, `pom.xml`,
`pyproject.toml` ni `uv.lock` hors des dossiers ignorés ; ce n'est sans doute
pas le dossier eqasim. Remède : faire pointer `model.root` vers le dossier qui
contient le code d'eqasim et son `config.yml`.

## `eqasim source changed after context sealing`

Cause : un fichier de code d'eqasim sous `model.root` (`*.py`, `*.java`,
`pom.xml`, `pyproject.toml`, `uv.lock`) a été ajouté, supprimé ou modifié après
la planification de la campagne. Le code fait partie de l'identité de la
campagne, et chaque run vérifie son empreinte avant de démarrer. Remède :
restaurer le code tel qu'il était au scellement du plan, ou planifier une
nouvelle campagne avec le nouveau code. Les fichiers écrits dans le
`working_directory` et l'`output_path` d'eqasim, ainsi que dans les
environnements virtuels ou conda, sont ignorés. Si un autre outil écrit des
fichiers de code dans le dossier eqasim, déplacez ses sorties ailleurs.

## L'identité de la campagne change après avoir lancé eqasim avec une autre configuration

Cause : seuls le `working_directory` et l'`output_path` de la configuration
désignée par `model.config` sont exclus de l'empreinte du code d'eqasim. Un
cache synpp écrit dans le dossier eqasim par une autre configuration (par
exemple une copie avec un autre `working_directory`) est pris dans l'empreinte
comme le code : ses étapes `matsim.*` clonent des projets Java contenant des
fichiers `*.java` et `pom.xml`. L'identité de la campagne change alors : `plan`
signale un plan différent et une campagne en cours s'arrête avec
`eqasim source changed after context sealing`. Remède : garder le
`working_directory` hors du dossier eqasim, ou utiliser le même
`working_directory` dans toutes les configurations lancées depuis ce dossier.

## `eqasim source contains a file name that is not valid UTF-8: ...`

Cause : un fichier de code sous `model.root` a un nom dont les octets ne sont
pas de l'UTF-8 valide (possible sous Linux) ; son chemin ne peut donc pas
entrer dans l'empreinte. Remède : renommer ou supprimer ce fichier ; le message
affiche le nom avec les octets invalides échappés.

## `eqasim source changed since plan (eqasimSourceSha256)` et messages voisins

Cause : à la reprise ou à la lecture d'une campagne (`run`, `check`, `estimate`,
`analyze`), le plan recalculé à partir
des entrées actuelles est comparé au `plan.parquet` scellé. Le message nomme ce
qui diffère : `eqasim source` (`eqasimSourceSha256`), `data manifest`
(`dataManifestSha256`), `lockfile` (`lockfileSha256`), `eqasim-sa version`
(`eqasimSaVersion`), la configuration eqasim (`eqasim config changed since
plan` : une clé autre que `data_path`, `processes`, les binaires,
`java_memory`, `output_path`, `working_directory` ou `random_seed` a été
modifiée), ou une sortie (`output 'age' changed since plan: file`,
`... separator`, `output '...' added since plan`,
`output '...' removed since plan`). `analyze` ne compare que la
méta-configuration au plan : modifier le code, la configuration, le `uv.lock`
ou le manifeste de données d'eqasim après les runs ne le bloque pas. Remède :
restaurer ce qui a changé, ou planifier une nouvelle campagne.
`plan.parquet has no sealed modelDocument; plan a new campaign` signifie que le
plan a été produit par une version antérieure d'eqasim-sa.

## `model config changed after context sealing`

Cause : la configuration eqasim désignée par `model.config` a été modifiée
pendant la campagne. Remède : la restaurer, ou planifier une nouvelle campagne
avec le fichier modifié.

## `output '...' is written by eqasim stage '...', which is not in run: of ...`

Cause : une sortie connue (par exemple `age`, `distance` ou `nbr_vehicle`)
garde son fichier par défaut, mais l'étape eqasim qui l'écrit ne figure pas dans
la liste `run:` de votre configuration eqasim. eqasim-sa exécute `run:`
exactement tel qu'il est écrit et n'ajoute aucune étape. Remède : ajouter
l'étape nommée, en général `analysis.synthesis.population`, à `run:` dans votre
copie de la configuration, avant de planifier. Voir
[Ce qu'eqasim exécute](yaml-reference.md#ce-queqasim-exécute).

## `output '...' is not a known eqasim output; declare its file`

Cause : un nom de sortie hors des douze sorties eqasim connues n'a pas de
`file`. Remède : ajouter `file`, relatif au dossier de sortie d'eqasim, par
exemple `file: "analysis_population/{prefix}summary.csv"`, et `separator` si ce
n'est pas une virgule. Voir
[Sorties connues](yaml-reference.md#sorties-connues-et-leurs-fichiers).

## `eqasim did not write declared output '...' (expected ...)`

Cause : un run s'est terminé mais le fichier déclaré n'est pas dans le dossier
de sortie d'eqasim. Raisons habituelles : l'étape qui l'écrit n'est pas dans
`run:`, le chemin `file` est mal orthographié, ou `{prefix}` ne correspond pas à
l'`output_prefix` de votre configuration eqasim. Remède : lire
`logs/<runId>.log`, corriger la configuration eqasim ou la déclaration `file`
(une nouvelle campagne est nécessaire si le plan était déjà scellé), puis
relancer.

## `share category must be a quoted string` ou `where values must be quoted strings`

Cause : YAML lit `category: 0`, `category: True` ou `zone: 44` comme un nombre
ou un booléen, alors que les libellés de classe et les cellules de table sont
comparés comme du texte. Remède : les écrire entre guillemets :
`category: "0"`, `category: "True"`, `where: {zone: "44"}`.

## `output '...' is missing declared column '...'` ou `column '...' must be numeric`

Cause : une colonne `category`, `simulated`, de référence, de `value` ou de
`where` ne correspond pas exactement à l'en-tête du CSV, ou une colonne
utilisée comme nombre contient du texte. Remède : afficher la première ligne du
fichier archivé (`head -n 1 archives/<runId>/age.csv`), corriger les noms de
colonnes sous `outputs` ou `indicators`, puis relancer `analyze`. Ces clés ne
sont pas scellées dans le plan : aucun run n'est à refaire.

## Relance d'`init` : `init inputs changed: ...` ou `foreign content in init project: ...`

Cause : relancer `init` sur un projet existant (relance identique ou
`--regenerate-data-manifest`) conserve les fichiers de départ et le README modifiés, mais
refuse ce qu'il ne peut pas reprendre (voir
[Relancer `init`](cli-reference.md#relancer-init-sur-un-projet-existant)). Le
message le nomme : `init inputs changed: model.root in configs/01-grid.yml`
quand une valeur remplie par `init` (`model.root`, `model.config`,
`model.data_manifest`, `execution.scratch`, `execution.invocation`,
`execution.machine_options.data_path`, `campaign`, `campaign_root`) diffère de
ce qu'écrirait cet `init`, parce qu'une option diffère du premier `init` ou que
la valeur a été modifiée (les valeurs sont comparées telles qu'écrites : un
`model.root` absolu équivalent est un changement) ; `init cannot parse
configs/<fichier>: ParserError at line 9, column 11: expected ',' or ']', but
got '<stream end>'` pour un fichier de départ qui n'est pas du YAML valide (la
classe peut aussi être `ScannerError`, selon l'endroit où l'analyseur
s'arrête) ; `foreign content in init project:
configs/01-grid-structured.yml` pour un autre fichier dans `configs/` ;
`foreign content in init project: campaigns/<nom>` pour un dossier de campagne
à vous ; `foreign content in init campaign <campagne>: notes.txt` pour un
fichier à vous dans un dossier de campagne ; `foreign content in init project:
configs/analyses/lien.py` pour un lien symbolique parmi vos scripts
d'analyse ; `foreign content in init project: scratch` quand le premier `init`
a placé le scratch dans le projet (le cas par défaut) et que `--scratch`
désigne maintenant un autre dossier. Rien n'est écrit. Remède : la relance n'est jamais nécessaire pour
continuer ; `plan`, `run`, `check`, `estimate` et `analyze` fonctionnent
toujours. Pour la relancer malgré tout, donnez les mêmes options qu'au premier
`init`, rétablissez la valeur nommée et sortez du projet les autres fichiers et
dossiers de campagne.

## `plan differs; choose a new campaign_root`

Cause : design, paramètres, contexte, version du paquet ou exigences
d'indicateurs différents sous une racine déjà scellée. Remède : donner à la
campagne un nouveau nom `campaign` (avec le `campaign_root` par défaut, elle
reçoit aussi un nouveau dossier), ou choisir un nouveau `campaign_root`. Ne
remplacez pas `plan.parquet`. Renommer une campagne change aussi sa
`design_seed` par défaut.

## `campaign_root contains durable state without a plan`

Cause : état résiduel ou contenu étranger dans la racine. Remède : identifier sa
provenance. Pour une nouvelle étude, choisir une racine vide. Ne fabriquez pas
un plan pour recouvrir un état existant.

## `campaign <campagne> has no plan yet: run plan first`

Cause : `check` ou `estimate` a été lancé sur une méta-configuration (cible ou
calibration) dont la campagne n'a pas encore de `plan.parquet`. Remède : lancez
`plan` sur la méta-configuration de la campagne nommée, puis relancez `check`
ou `estimate`.

## `no completed calibration run yet: run ... first`

Cause : `estimate`, ou l'élément `diskSpace` de `check`, n'a trouvé aucun run
terminé à mesurer, ni dans les campagnes de calibration (celles de
`calibration.meta_configs` et de `--calibration-meta-config`) ni dans la
campagne elle-même. Le message dit quoi lancer :

- `run campaign <campagne> first` : aucune campagne de calibration n'est
  référencée, donc les runs terminés de la campagne elle-même sont mesurés.
  C'est le cas du fichier de départ de calibration `configs/00-calibration.yml`
  lui-même.
- `run the calibration campaign <campagne> first` (ou `run one of the
  calibration campaigns <a>, <b> first`) : la calibration référencée n'a encore
  aucun run terminé ; un seul run terminé dans l'une d'elles suffit.

Remède : lancer `plan`, puis `run` sur la campagne nommée. Relancer ensuite
`check` et `estimate` (après `plan` pour une cible qui n'en a pas encore).

## `unknown factor` ou `reserved or infrastructure factor`

Cause : nom absent du bloc `config:` eqasim, option dédiée, infrastructure ou
colonne réservée. Remède : utiliser une clé scientifique active ; placer seulement
les options d'environnement autorisées dans `execution.machine_options`.

## `... keys must be among ...`

Cause : une clé est mal orthographiée, mal placée, ou n'appartient pas à la
méthode choisie (par exemple `bootstrap_resamples` dans un bloc `analysis` de
Grid). Le message liste les clés permises à ce niveau ; `missing required`
nomme une clé requise absente et `unknown` la clé refusée. Remède : corriger la
clé comme décrit dans la [référence YAML](yaml-reference.md). Une fois le
fichier chargé, `"${SA[@]}" show-config FICHIER` affiche la configuration
complète qu'utilise eqasim-sa.

## `declare simulation seeds in experiment.seeds, not parameters.seed`

Cause : un paramètre nommé `seed` est déclaré sous `parameters`. Remède :
déplacer les valeurs dans `experiment.seeds`. Pour Sobol et Sobol multi-scramble,
elles deviennent le facteur `seed` ; pour les autres méthodes, ce sont des
réplications.

## `factor ... requires a distribution under a scientific charter`

Cause : un paramètre utilise la forme courte `nom: [a, b]` (ou `values` seul)
dans une campagne avec `charter.status: scientific`. Remède : utiliser la forme
longue avec `values` et `distribution`, comme décrit dans la
[référence YAML](yaml-reference.md#sous-une-charte-scientifique). Pour Sobol
multi-scramble, le facteur seed exige aussi `experiment.seeds_provenance`.

## Erreur de type dans `parameters`

Cause fréquente : booléen, `null`, niveaux mixtes, float non fini ou domaine
OAT discret mal ordonné. Remède : conserver un type homogène et des nombres finis.
Pour un float requis, comme un `threshold` ou une `reserve_fraction` de charte,
écrire un point décimal (`1.0`, pas `1`) ; seul
`analysis.completeness_threshold` accepte aussi `0` et `1`.

## Erreurs de facteur structuré nommé

- `factor 'matching_attributes' must contain exactly ...` : une déclaration de
  démonstration contient exactement `configurations` ; une déclaration scientifique
  contient exactement `configurations` et `distribution`. Ne mélangez pas
  `values` et `configurations`.
- `unknown factor` ou `model config value must be a list of strings` : le YAML
  modèle scellé doit déjà contenir `config.matching_attributes` active sous forme
  de liste de chaînes. Écrivez-la dans votre configuration eqasim avant de
  planifier ; eqasim-sa ne crée ni ne convertit cette clé.
- `must be a non-empty ordered list...`, `distinct payloads` ou `payload aliases` :
  chaque charge est une liste non vide de chaînes non vides uniques, et deux
  labels ne peuvent résoudre vers la même liste ordonnée. Les labels sont aussi
  des chaînes non vides sans NUL.
- `does not support structured factors` : `discrete_oat` et
  `canonical_morris` refusent tous les charges structurées nommées. Utilisez Grid
  pour une démonstration, ou un `multiscramble_sobol` entièrement gouverné si les
  probabilités égales des labels sont scientifiquement justifiées.
- `structured plan metadata ...`, `mismatch` ou `unknown label` : dans le schéma
  v3, `structuredFactorsVersion` et la métadonnée canonique `structuredFactors`
  sont scellés avec les labels et identités. Préservez l'état endommagé pour
  diagnostic et créez un plan sous une nouvelle racine ; ne modifiez jamais
  `plan.parquet`.
- L'absence de `--matching_attributes` dans la commande modèle est volontaire.
  Les valeurs composites sont écrites dans le snapshot YAML de chaque run, pas
  transportées par le CLI scalaire de synpp. Inspectez le `config.yml` de la
  tentative en échec au lieu d'ajouter un override CLI manuel contenant la liste.

La procédure sûre complète figure dans le
[tutoriel des configurations structurées](structured-configurations-tutorial.md).

## `canonical_morris requires charter.status scientific`

Cause : Morris canonique est configuré comme démonstration ou sans charte.
Remède : ne transformez pas mécaniquement le fichier de départ OAT discret généré. Rédigez
une charte scientifique, des distributions, un budget, des critères d'arrêt et
des contrôles de robustesse propres à l'étude selon la
[référence YAML](yaml-reference.md#morris-canonique).

## Bloc `stochastic` absent ou incompatible

Toute charte scientifique exige un mode exact. Morris canonique exige
`conditional_mean`, des CRN `within_trajectory` et une allocation ; Sobol
multi-scramble exige `finite_seed_factor` sans allocation. Un pilote a sélection
et provenance nulles et au moins son minimum de seeds. La production a une
provenance pilote non vide et exactement son nombre sélectionné. Ne réparez pas
un pilote en ajoutant des seeds ou en changeant sa phase : choisissez une nouvelle
racine de campagne.

## Allocation `capped` ou `infeasible`

`capped` signifie que la formule MCSE demande plus que
`maximum_replications` ; la cible n'est donc pas démontrée. `infeasible` signifie
que même les positions structurelles sélectionnées `T*(D+1)*R` dépassent le
budget hors réserve. Révisez précision, plafond, design ou budget dans une
nouvelle campagne ; n'abaissez jamais le besoin et ne comptez pas les runs
dédupliqués.

## Erreur d'objectif, de dépendance ou de budget Morris canonique

Morris canonique accepte uniquement l'objectif `screening`, une indépendance des
paramètres et un contrat scientifique complet. Ses positions structurelles du
design, sans déduplication des run IDs, doivent tenir dans
`floor(maximum_runs * (1 - reserve_fraction))` ; la réserve n'est pas disponible
au plan initial. Il impose aussi au plus 500 candidats et un budget
déterministe de calcul des distances. Réduisez le design ou augmentez un budget
scientifiquement justifié ; ne rebaptisez pas FP ou FF en criblage.

## Erreur de point de contrôle ou de bootstrap Morris canonique

Utilisez au moins 100 rééchantillonnages. Les points de contrôle sont des entiers triés,
uniques, au moins égaux à 2 et se terminent par `trajectories` ;
`screening_top_k` ne dépasse pas le nombre de facteurs. Ces diagnostics ne
certifient pas à eux seuls l'arrêt ou la robustesse de la charte.

## `unsupported experiment.method`

Les méthodes prises en charge sont `grid`, `discrete_oat`, `canonical_morris`,
`sobol` et `multiscramble_sobol`. Utilisez `discrete_oat` pour une
démonstration à niveaux adjacents, ou rédigez une campagne de criblage scientifique `canonical_morris`
propre à l'étude.

## `duplicate canonical configuration and seed pair`

Cause : plusieurs positions Grid résolvent vers le même couple configuration et
seed, souvent à cause de niveaux dupliqués. Remède : dédupliquer les domaines ou
séparer réellement les configurations.

## `campaign completeness ... is below threshold`

Cause : marqueurs absents ou logs d'échec. Remède : consulter
`logs/<runId>.log`, corriger le pipeline et relancer `run`. Ne créez pas de
marqueur à la main. Pour examiner entre-temps les runs terminés,
`analyze --descriptive-only` publie leurs sorties descriptives, sans table de
sensibilité (voir [`analyze`](cli-reference.md#analyze)).

## `invalid completion marker`

Cause : fichier manuel, tronqué, mauvais schéma, mauvais contexte, compression
incorrecte ou contenu incohérent. Remède : préserver l'état pour diagnostic,
puis reconstruire le run par la procédure normale dans une racine saine. Une
édition manuelle rend la preuve de complétude non fiable.

## Un second `run` semble bloqué

Cause : un autre `run` est actif sur la même campagne. Il n'y a pas de message
d'erreur : le second `run` attend les slots de worker et les runs que tient le
premier, puis saute les runs terminés entre-temps (`skipped`). Remède : laisser
le premier se terminer, ou arrêter le second avec Ctrl+C. Après un crash, les
verrous sont libérés automatiquement par le système d'exploitation ; ne
supprimez pas les fichiers de verrou pendant une exécution.

## `static check time budget exhausted`

Cause : empreinte du code d'eqasim (signalée alors par `model source
fingerprint time budget exhausted`), lecture des plans/manifeste, sonde synpp ou
mesure disque trop lente au-delà du budget de 55 secondes. Remède : vérifier les stockages réseau,
la taille des fichiers de contrôle et le temps de démarrage de l'environnement.

## `synpp environment probe failed`

Cause : l'invocation se termine par `-m synpp` mais son interpréteur ne démarre
pas ou ne peut pas faire `import synpp` (la sonde exécute l'invocation en
remplaçant `-m synpp` par `-c "import synpp"` et exige le code de sortie 0 ;
aucune sortie de synpp n'est lue). Remède : exécuter cette commande dans
`model.root`, puis vérifier l'environnement et les versions. La sonde statique ne lance pas une simulation complète.
Quand l'invocation ne se termine pas par `-m synpp`, le contrôle a
`status: skipped` et une `note` ; il ne rend pas le résultat invalide.

## Échec de `check --smoke`

Cause : moins de deux seeds distinctes ou absence de `sampling_rate`
(`smoke requires sampling_rate and two distinct explicit seeds`), pipeline réel
en échec, fichier de sortie déclaré non écrit. Remède : satisfaire le contrat
smoke et lire le détail retourné. Attention : cette option exécute deux
simulations temporaires à `sampling_rate: 0.001` ; elle consomme du calcul et
accède aux données réelles. Les runs du smoke utilisent la configuration du
premier run du plan avec `execution.machine_options` appliqué, comme `run` : le
dossier de données qu'ils lisent est `execution.machine_options.data_path`
quand il est renseigné. Les runs de smoke ignorent les indicateurs qui lisent une
[sortie d'analyse](analysis-scripts.md) : `check` n'exécute jamais les scripts
d'analyse, si bien qu'un script défaillant n'apparaît qu'à `analyze`.

## Espace disque ou scratch insuffisant

Cause : `missingDiskBytes` ou `missingScratchBytes` positif ; l'élément
`diskSpace` de `check` échoue alors avec `insufficient disk space: durable
missing N bytes; scratch missing M bytes`. Remède : augmenter
le quota, déplacer `campaign_root` ou `execution.scratch`, ou réduire
`execution.workers`. Le scratch estimé est le maximum observé par worker multiplié
par les workers.

## `meta-config changed while preparing execution`

Cause : le YAML a changé pendant `run`, ou entre soumission et job SLURM.
Remède : arrêter les éditeurs/générateurs concurrents et relancer avec un fichier
stable. Après changement de design ou contexte, choisir une nouvelle racine.

## Variance Sobol constante ou non représentable numériquement

Pour `sobol` de démonstration, toutes les valeurs d'indicateur peuvent être constantes
ou les rééchantillonnages bootstrap peuvent être dégénérés. Vérifiez
l'indicateur, les domaines, `N` et la diversité effective des runs. Ne masquez
pas l'erreur statistique.

Avec `multiscramble_sobol`, une variance non positive ou un contraste A/B
inefficace à un scramble et à un point de contrôle quelconques est fatal. Augmentez la
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

## Nom de campagne ou de sortie refusé

Les noms de campagne sont limités à 64 caractères. Sur tous les systèmes,
eqasim-sa refuse les noms réservés Windows (`CON`, `PRN`, `AUX`, `NUL`,
`COM1`-`COM9`, `LPT1`-`LPT9`, avec ou sans extension, quelle que soit la casse),
les noms terminés par un point ou une espace, et les noms qui ne diffèrent que par
la casse. Une campagne créée sous Linux reste ainsi transférable vers Windows et
macOS. Renommez la campagne, la sortie ou l'indicateur.

## Fins de ligne et `.gitattributes`

Copier ou extraire des fichiers sous Windows peut convertir LF en CRLF.
eqasim-sa hache les entrées texte après normalisation CRLF vers LF : une copie
en CRLF produit donc les mêmes identités que sur le cluster. Cela concerne
l'empreinte du code d'eqasim (`eqasimSourceSha256`), le hash de `uv.lock`
(`lockfileSha256`), la forme JSON canonique du manifeste de données
(`dataManifestSha256`), l'identité logicielle d'`eqasim-sa` et les définitions
des indicateurs intégrés. La configuration eqasim dans les preuves d'exécution
(`model_config_sha256`) est le hash de sa forme scellée (`modelDocument`),
indépendant des fins de ligne et de la mise en forme. Les fichiers de données
restent hachés octet pour octet. `init` écrit aussi un `.gitattributes` (sans jamais écraser le
vôtre) qui impose LF pour les fichiers YAML, JSON et `*.lock`. Si `check`
signale une divergence après un transfert, comparez les fichiers sur les deux
machines avant de soupçonner les données.

## Backend SLURM sous Windows ou macOS

Le backend `slurm` exige Linux. `check` signale l'élément `backend` en échec avec
`the slurm backend requires Linux; run eqasim-sa on the cluster`, et `run` refuse
avec le même message. Préparez l'étude sur le PC, copiez-la vers le cluster par
`scp`, `sftp` ou tout autre outil de copie, refaites le rendu des chemins machine (marqueurs
`__MACHINE_*__` de `protocols/*/` avec `render_protocol.py`, ou édition des chemins
du YAML), puis lancez `plan`, `check` et `run` sur le cluster. Le PC ne soumet
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
| `model configuration changed` | `model_config_sha256` | le fichier `model.config` d'eqasim a été modifié depuis les premiers runs (hors clés propres à la machine comme `data_path` ou `processes`) |
| `eqasim-sa installation changed` | `eqasim_sa_sha256` | `eqasim-sa` a été mis à jour ou réinstallé |

`environment differs` est suivi des noms des variables ajoutées ou retirées
depuis les premiers runs, par exemple `environment differs (variables added:
UV, UV_RUN_RECURSION_DEPTH)`. Les valeurs ne sont jamais affichées, car elles
peuvent contenir des secrets. Les runs enregistrent le nom de chaque variable
mais une seule empreinte de toutes les valeurs : quand les noms sont les mêmes,
le message indique `same variable names, some values differ` sans nommer la
variable (souvent `PATH`) ; quand des noms ont été ajoutés ou retirés, une
valeur modifiée ne peut pas être distinguée.

Remède : préparer une nouvelle campagne (nouveau `campaign_root`) sur cette
machine, ou rétablir ce qui a changé. Pour le backend `slurm`, `same_machine` est
ignoré sur la machine qui soumet et appliqué dans le job Slurm.

### Reprendre depuis un autre shell ou un IDE

L'identité de la machine inclut les variables d'environnement du processus qui
exécute eqasim-sa : leurs noms et leurs valeurs. `VIRTUAL_ENV` ne compte pas :
eqasim-sa la retire de l'environnement d'eqasim et des scripts d'analyse, car
elle désigne l'environnement d'eqasim-sa lui-même. Toutes les autres variables
comptent, sauf celles-ci, propres à la session : `DISPLAY`, `HOSTNAME`, `ITERM_SESSION_ID`, `OLDPWD`, `PWD`, `SECURITYSESSIONID`, `SHLVL`, `SSH_AUTH_SOCK`, `SSH_CLIENT`, `SSH_CONNECTION`, `SSH_TTY`, `TEMP`, `TERM_PROGRAM_VERSION`, `TERM_SESSION_ID`, `TMP`, `TMPDIR`, `WINDOWID`, `WT_PROFILE_ID`, `WT_SESSION`, `XDG_SESSION_ID`, `XDG_VTNR`, `_`, et toute variable dont le nom
commence par `SLURM_` ou `SUBMITIT_`. Ouvrir un nouveau terminal du même type ne
déclenche donc pas le refus, mais reprendre `run` depuis un environnement
différent peut le déclencher : autre `PATH` (activer ou désactiver un
environnement virtuel le modifie), ou terminal d'IDE qui ajoute ses propres variables
(par exemple `TERM_PROGRAM` ou `VSCODE_*`) alors que la campagne a démarré dans
un terminal ordinaire.

Pour reprendre de façon cohérente :

- démarrez et reprenez une campagne depuis le même type de terminal (la même
  application de terminal, ou le même IDE), avec le même environnement virtuel
  activé, ou aucun ;
- lancez eqasim-sa avec la même commande à chaque fois, par exemple le tableau
  `SA` du README généré ;
- avant de reprendre, lancez `check` sur la méta-configuration : son élément
  `same_machine` indique, sans rien exécuter, si `run` accepterait de reprendre ;
- pour trouver la variable qui diffère, enregistrez l'environnement avant le
  premier `run` et comparez-le plus tard : `env | sort > env-start.txt` puis
  `env | sort | diff env-start.txt -` sous Linux et macOS ;
  `Get-ChildItem Env: | ForEach-Object { "$($_.Name)=$($_.Value)" } | Sort-Object | Set-Content env-start.txt`
  puis
  `Compare-Object (Get-Content env-start.txt) (Get-ChildItem Env: | ForEach-Object { "$($_.Name)=$($_.Value)" } | Sort-Object)`
  dans PowerShell sous Windows.

## `analysis '...' failed for run ...`

Message complet : `analysis '<nom>' failed for run <runId>: <dernière ligne de
l'erreur>`. Cause : le script d'analyse a levé une erreur, ou n'a pas respecté
son contrat, pour ce run. Toute erreur de script arrête tout `analyze`, même
avec `completeness_threshold` inférieur à 1. La sortie d'erreur complète, avec
la trace Python et ce que le script a affiché, se trouve dans
`<campaign_root>/analyses-cache/logs/<nom>-<runId>.log`. Dernières lignes
fréquentes :

| Dernière ligne | Cause et remède |
| --- | --- |
| `TypeError: analysis script must define compute(files, parameters)` | le script n'a pas de fonction `compute`, ou son nom est mal orthographié |
| `TypeError: compute must return a pandas.DataFrame, not ...` | renvoyer un `pandas.DataFrame`, pas un dictionnaire, une liste ou un nombre |
| `ValueError: compute must return a table with at least one column` | la table renvoyée n'a aucune colonne |
| `ValueError: compute returned a duplicate column name: ...` ou `compute must return non-empty text column names, not ...` | renommer les colonnes : chaînes uniques et non vides |
| `ModuleNotFoundError: No module named ...` | un module absent de l'environnement d'eqasim, ou un module utilitaire placé à côté du script : le script s'exécute depuis une copie et ne peut pas l'importer ; recopiez le code dans le script |
| `KeyError: ...` | un nom absent de `files` (il doit figurer dans `inputs`) ou une colonne absente du fichier ; vérifiez le séparateur (`;` pour les fichiers de population) |

Remède : essayer le script à la main sur l'archive de ce run (voir
[Essayer un script à la main](analysis-scripts.md#essayer-un-script-à-la-main)),
le corriger, puis relancer `analyze` ; les résultats déjà calculés sont
réutilisés.

## `analyses need execution.python because execution.invocation does not end with -m synpp`

Cause : la méta-configuration déclare `analyses` et son `execution.invocation`
ne se termine pas par `-m synpp` : eqasim-sa ne peut pas en déduire le Python
qui exécute les scripts. Remède : ajouter `execution.python`, la commande de
l'interpréteur d'eqasim, par exemple `python: [uv, run, --locked, python]` ou
le chemin du `python` de l'environnement virtuel d'eqasim. Voir
[Quel Python exécute le script](analysis-scripts.md#quel-python-exécute-le-script).

## `analysis interpreter probe failed: ...`

Cause : avant d'exécuter un script, `analyze` lance une fois l'interpréteur
d'analyse pour lire les versions de Python, pandas, numpy, pyarrow, geopandas, shapely et pyogrio, et ce
lancement a échoué ; le message se termine par la dernière ligne de sa sortie
d'erreur (par exemple pandas absent, ou `uv` incapable de créer
l'environnement d'eqasim). Remède : vérifier que la commande
d'`execution.python` (ou celle par défaut) fonctionne dans le dossier eqasim,
par exemple `uv run --locked python -c "import pandas"` depuis `model.root`.

## Erreurs de déclaration d'une analyse

Ces messages arrêtent le chargement de la méta-configuration ; chacun nomme
l'analyse :

| Message | Remède |
| --- | --- |
| `analysis 'commute' input 'trips_spatial' is not declared in outputs` | déclarer la sortie sous `outputs` ; après les runs, seules les sorties déjà archivées peuvent être lues |
| `analysis 'commute' input 'mode_share' is an analysis; inputs must be outputs declared in outputs` | une analyse ne peut pas lire une autre analyse ; lire les sorties archivées dans un seul script |
| `analysis 'commute' input 'x' file '...' must be a .csv or .gpkg file` | les scripts ne lisent que des fichiers `.csv` et `.gpkg` |
| `analysis 'commute' script '...' does not exist` | le chemin est relatif au fichier YAML (ou absolu) |
| `analysis 'commute' script must be a .py file`, `... must be a regular file`, `... must not be a symlink` | faire pointer `script` sur le fichier `.py` lui-même |
| `analysis name 'trips' is already an output name` | donner un autre nom à l'analyse |
| `analysis name 'logs' is reserved for the analyses cache logs` | donner un autre nom à l'analyse |
| `primary_indicator must not use analysis output 'commute'` | l'indicateur principal doit lire une sortie archivée ; garder l'indicateur d'analyse comme indicateur secondaire |

## `analysis cache entry ... was altered; it was removed and recomputed`

C'est un avertissement du champ `warnings` du résultat d'`analyze`, pas une
erreur. Cause : un résultat en cache sous `analyses-cache/` ne correspondait
plus à son SHA-256 enregistré (fichier modifié ou endommagé). Il a été
supprimé puis recalculé à partir des archives ; l'analyse publiée utilise le
résultat recalculé. Rien à faire ; tout le dossier `analyses-cache/` peut être
supprimé quand aucune commande ne tourne.

## Analyser sur une autre machine

`analyze` n'a besoin ni de l'arborescence de données ni du scratch, et ne compare
jamais la preuve d'exécution à la machine qui analyse. Il faut : le dossier de
campagne rapatrié (`scp` ou `sftp`), la méta-configuration avec les
chemins machine modifiés pour cette machine, le dossier eqasim (son code, sa
configuration et son `uv.lock` peuvent différer de ceux des runs : `analyze`
vérifie la méta-configuration par rapport à ce que `plan.parquet` a scellé) et
un fichier manifeste de données (un petit fichier JSON ; l'arborescence de
données elle-même n'est pas nécessaire). `metaConfigSha256` est une provenance
publiée, pas une contrainte d'égalité : des chemins machine différents ne
bloquent pas l'analyse. Avec des [scripts d'analyse](analysis-scripts.md), il
faut aussi les scripts et l'environnement Python d'eqasim, qui les exécute
(`uv run --locked` le crée à la première utilisation).

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
campagnes (verrous et publication atomique non garantis). Ils ne sont pas non
plus détectés : eqasim-sa ne les refuse pas, vérifiez donc où se trouvent ces
chemins.

## Avertissements de sécurité

- Ne modifiez jamais `plan.parquet`.
- Ne créez, ne copiez et ne modifiez jamais `runs/<runId>.parquet`.
- Ne supprimez pas locks, scratch ou campagne pendant un processus ou job actif.
- Ne réutilisez pas une racine après changement de modèle, données, lockfile ou version.
- Ne considérez jamais une archive seule comme un run complet.

Pour les définitions de marqueur, archive, scratch et option machine, voir le
[glossaire](glossary.md).
