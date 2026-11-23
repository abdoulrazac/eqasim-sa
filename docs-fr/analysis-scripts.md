# Écrire un script d'analyse

[English](../docs/analysis-scripts.md) | **Français**

Au moment de lancer une campagne, on ne sait pas toujours quelles grandeurs on
voudra étudier. Un script d'analyse permet d'en ajouter une après coup : un
court fichier Python lit les fichiers archivés de chaque run et renvoie un
tableau. Ce tableau devient une *sortie d'analyse*, que les indicateurs
`tv_distance`, `share` et `value` lisent comme n'importe quelle autre sortie.

Le calcul d'une sortie d'analyse ne lance jamais eqasim ni synpp, ne lit jamais
le cache de synpp et n'a besoin d'aucune donnée d'entrée : il ne lit que les
archives des runs. Le plan et les archives ne sont jamais modifiés. On peut donc
exécuter toutes les simulations sur un supercalculateur, rapatrier le dossier de
campagne sur son ordinateur, y écrire des scripts et lancer `analyze`.

## Sommaire

- [Avant les runs : archiver assez](#avant-les-runs--archiver-assez)
- [Un exemple complet](#un-exemple-complet)
- [Le contrat du script](#le-contrat-du-script)
- [Règles pour un script](#règles-pour-un-script)
- [Quel Python exécute le script](#quel-python-exécute-le-script)
- [Ce que fait `analyze`](#ce-que-fait-analyze)
- [Essayer un script à la main](#essayer-un-script-à-la-main)
- [Cache et recalcul](#cache-et-recalcul)
- [Après une campagne sur un supercalculateur](#après-une-campagne-sur-un-supercalculateur)
- [Ce qui est enregistré](#ce-qui-est-enregistré)
- [Limites](#limites)

## Avant les runs : archiver assez

Un script ne peut lire que les fichiers que chaque run a archivés, c'est-à-dire
les sorties déclarées sous [`outputs`](yaml-reference.md#outputs) avant de
planifier la campagne. Ajouter une sortie ensuite demande une nouvelle
campagne. Archivez donc largement dès le départ : la population (`persons`,
`households`), les trajets et activités (`trips`, `activities`) et, si vous
risquez d'avoir besoin de distances ou de lieux, les fichiers spatiaux écrits
par eqasim (les fichiers de départ archivent `activities_spatial`, pas
`trips_spatial`) :

```yaml
outputs:
  persons: {}
  households: {}
  trips: {}
  activities: {}
  trips_spatial:
    file: "{prefix}trips.gpkg"
  activities_spatial:
    file: "{prefix}activities.gpkg"
```

eqasim écrit les fichiers `.gpkg` quand `output_formats` de sa configuration
contient `gpkg`, ce qui est sa valeur par défaut.

## Un exemple complet

Cet exemple ajoute à une campagne Grid la distance moyenne domicile-travail de
chaque run. Il fonctionne tel quel avec les fichiers de départ. Le projet se présente
ainsi :

```text
mon-analyse-sa/
├── configs/
│   ├── 01-grid.yml
│   └── analyses/
│       └── commute.py
└── campaigns/
    └── mon-analyse-sa-grid/
```

Ajouter `analyses` à `configs/01-grid.yml` modifie un fichier de départ : une relance
ultérieure d'`init` conserve le fichier tel quel et le liste dans `kept`, comme
le dossier `configs/analyses/`, qu'elle accepte sans le lire (voir
[Relancer `init`](cli-reference.md#relancer-init-sur-un-projet-existant)).
Seul le changement des valeurs remplies par `init` est refusé.

La méta-configuration garde les sorties du fichier de départ Grid, qui archivent déjà
tout ce que lit le script, et déclare l'analyse et les indicateurs :

```yaml
outputs:                               # les sorties du fichier de départ Grid, inchangées
  persons: {}
  households: {}
  trips: {}
  activities: {}
  activities_spatial:
    file: "{prefix}activities.gpkg"
  age:
    category: Age class
    simulated: Proportion_EQASIM
    references: [INSEE]
  distance:
    category: Distance class
    simulated: Proportion_EQASIM
    references: [HTS]
  nbr_vehicle:
    category: Number of vehicles class
    simulated: Proportion_EQASIM
    references: [HTS]
analyses:
  commute:
    script: analyses/commute.py        # relatif à ce fichier YAML
    inputs: [trips, activities_spatial, persons]   # sorties archivées transmises au script
indicators:
  - tv_distance: {outputs: [age, distance, nbr_vehicle]}   # indicateurs par défaut du fichier de départ, conservés
  - value: {output: commute, column: mean_commute_km, where: {group: all}}
```

> **Déclarer un bloc `indicators` remplace les indicateurs par défaut.** Sans
> ce bloc, chaque sortie qui a des références reçoit un indicateur
> `tv_distance` ; avec lui, seuls les indicateurs listés sont calculés. La
> première ligne ci-dessus garde les trois indicateurs `tv_distance` du
> fichier de départ ; supprimez-la et ils disparaissent des résultats.

La [référence YAML](yaml-reference.md#analyses) décrit chaque clé
d'`analyses`, et son [exemple complet](yaml-reference.md#grid-avec-un-script-danalyse)
montre un fichier entier.

`configs/analyses/commute.py` mesure la distance à vol d'oiseau de chaque
trajet du domicile au travail, en kilomètres, et en fait la moyenne pour
l'ensemble des actifs qui se déplacent et pour chaque sexe. Le fichier des
trajets n'a pas de géométrie : chaque trajet désigne ses activités précédente et
suivante (`preceding_activity_index`, `following_activity_index`), dont les
lieux sont dans le fichier spatial des activités. Le script joint les deux pour
obtenir les points du domicile et du travail :

```python
"""Mean home-to-work distance of one run, for all commuters and by sex."""

import geopandas
import pandas


def compute(files, parameters):
    trips = pandas.read_csv(
        files["trips"],
        sep=";",
        usecols=[
            "person_id",
            "preceding_activity_index",
            "following_activity_index",
            "preceding_purpose",
            "following_purpose",
        ],
    )
    activities = geopandas.read_file(files["activities_spatial"])
    persons = pandas.read_csv(files["persons"], sep=";", usecols=["person_id", "sex"])

    if activities.crs is None or not activities.crs.is_projected:
        raise ValueError("activities_spatial must use projected coordinates in metres")

    # A trip goes from its preceding activity to its following activity:
    # join both to the activity places to get the home and work points.
    commutes = trips[
        (trips["preceding_purpose"] == "home") & (trips["following_purpose"] == "work")
    ]
    places = activities[["person_id", "activity_index", "geometry"]]
    commutes = commutes.merge(
        places,
        left_on=["person_id", "preceding_activity_index"],
        right_on=["person_id", "activity_index"],
    ).merge(
        places,
        left_on=["person_id", "following_activity_index"],
        right_on=["person_id", "activity_index"],
        suffixes=("_home", "_work"),
    )
    if commutes.empty:
        raise ValueError("no home-to-work trip in this run")

    home = geopandas.GeoSeries(commutes["geometry_home"], crs=activities.crs)
    work = geopandas.GeoSeries(commutes["geometry_work"], crs=activities.crs)
    commutes = pandas.DataFrame(
        {
            "person_id": commutes["person_id"].to_numpy(),
            "distance_km": home.distance(work).to_numpy() / 1000.0,
        }
    ).merge(persons, on="person_id")

    groups = [("all", commutes)] + [
        (str(sex), table) for sex, table in commutes.groupby("sex")
    ]
    return pandas.DataFrame(
        {
            "group": [name for name, _ in groups],
            "commuters": [len(table) for _, table in groups],
            "mean_commute_km": [table["distance_km"].mean() for _, table in groups],
        }
    )
```

Pour un run d'une petite population nantaise (`sampling_rate: 0.001`,
seed 1201), il a renvoyé, avec une configuration eqasim et un jeu de données
particuliers :

```text
group,commuters,mean_commute_km
all,478,9.775963185677492
female,218,9.37974123148458
male,260,10.108180054962318
```

L'indicateur `value(commute, mean_commute_km, group=all)` lit la première
ligne : 9,78 km pour ce run. Ces chiffres sont indicatifs : le même
`sampling_rate` et la même seed donnent d'autres valeurs avec une autre
configuration eqasim (autres paramètres de matching, autres millésimes de
données comme l'année des IRIS ou de la BPE) ou d'autres données d'entrée.

### Seulement avec `mode_choice: true` : part des trajets par mode

Cette seconde analyse a besoin d'une colonne `mode` dans le fichier des
trajets, qu'eqasim n'écrit que si sa configuration fixe `mode_choice: true`.
La plupart des configurations officielles d'eqasim (`config.yml`,
`config_nantes.yml`, `config_lyon.yml`, `config_toulouse.yml`,
`config_corsica.yml`) fixent `mode_choice: false` ; `config_full.yml` le fixe à
`true`. Ajoutez cette analyse seulement si votre copie le fixe à `true` ;
sinon le script s'arrête avec le message qu'il lève et `analyze` échoue
(voir [Ce que fait `analyze`](#ce-que-fait-analyze)). Ajoutez à l'exemple
complet ci-dessus :

```yaml
analyses:
  mode_share:
    script: analyses/mode_share.py
    inputs: [trips]
    category: mode                     # colonnes du tableau renvoyé par le script
    simulated: share
indicators:
  - share: {output: mode_share, category: car}
```

`configs/analyses/mode_share.py` compte les trajets de chaque mode :

```python
"""Share of trips by mode in one run."""

import pandas


def compute(files, parameters):
    trips = pandas.read_csv(files["trips"], sep=";")
    if "mode" not in trips.columns:
        raise ValueError(
            "trips has no mode column; set mode_choice: true in the eqasim configuration"
        )
    counts = trips["mode"].astype(str).value_counts().sort_index()
    return pandas.DataFrame(
        {"mode": counts.index, "share": counts.to_numpy() / counts.sum()}
    )
```

Comme l'analyse déclare
`category: mode` et `simulated: share`, l'indicateur `share(mode_share, car)`
lit la part des trajets en voiture. Avec une colonne de référence dans le
tableau renvoyé (par exemple des parts d'enquête écrites comme constantes dans
le script) et `references` dans l'analyse, `tv_distance` comparerait les deux
distributions.

## Le contrat du script

Un script est un fichier `.py` qui définit une fonction :

```python
def compute(files, parameters):
    ...
    return table  # un pandas.DataFrame
```

`files` associe à chaque nom listé dans `inputs` le chemin d'une copie du
fichier archivé, par exemple `{"trips": ".../trips.csv",
"activities_spatial": ".../activities_spatial.gpkg", "persons": ".../persons.csv"}`. La copie garde le nom de l'archive,
`<nom de la sortie><extension>`. Lisez le fichier avec le séparateur
qu'eqasim a utilisé : `;` pour les fichiers de population (`persons`,
`trips`...), `,` pour les fichiers d'`analysis_population` ;
`geopandas.read_file` lit les fichiers `.gpkg`.

`parameters` décrit le run :

```python
{
    "runId": "0e599693c300002e9a1c0dffe06ff0ff",
    "seed": 1201,
    "factors": {"sampling_rate": 0.001},
}
```

`factors` donne la valeur de chaque paramètre varié ; pour une
[configuration nommée](yaml-reference.md#configurations-nommées), il donne le
libellé, pas le contenu. La plupart des scripts ignorent `parameters` ; ils
servent quand le calcul dépend du run (par exemple pour rapporter un effectif
au `sampling_rate`).

La valeur renvoyée doit être un `pandas.DataFrame` d'au moins une colonne, aux
noms de colonnes uniques, non vides et textuels. eqasim-sa l'écrit en CSV
(séparateur virgule, sans index, UTF-8) puis le relit avec les mêmes règles que
les autres sorties : les cellules sont lues comme du texte, et les colonnes
qu'un indicateur utilise comme nombres sont converties explicitement. Une
valeur manquante (`NaN`) devient une cellule vide, que `value`, `share` et
`tv_distance` refusent avec une erreur nommant la colonne ; levez plutôt une
erreur claire dans le script, comme le fait `commute.py` quand un run n'a aucun
trajet domicile-travail.

## Règles pour un script

1. **Ne pas importer de modules d'eqasim.** L'empreinte d'un résultat couvre le
   script, ses entrées, les paramètres du run et l'environnement Python, pas le
   code d'eqasim. Un script qui importerait du code d'eqasim ne serait pas
   recalculé quand ce code change. Utilisez pandas, numpy, geopandas et la
   bibliothèque standard.
2. **Ne lire que les fichiers de `files`.** Un fichier lu ailleurs ne fait pas
   partie de l'empreinte : ses modifications passeraient inaperçues. Les
   constantes écrites dans le script (valeurs de référence, bornes de classes)
   sont permises : elles font partie du script.
3. **Ne rien écrire : renvoyer le tableau.** Les fichiers d'entrée sont des
   copies temporaires, supprimées à la fin du script, et rien de ce qui est
   écrit ailleurs n'est conservé ni tracé. Les archives ne sont jamais exposées
   au script.
4. **Garder le script en un seul fichier.** eqasim-sa exécute une copie du
   script depuis un dossier temporaire : le dossier du script n'est donc pas
   dans `sys.path`, et un module utilitaire placé à côté ne peut pas être
   importé. Recopiez les fonctions communes dans chaque script.
5. **Renvoyer le même tableau pour les mêmes entrées.** Les résultats sont
   mis en cache et réutilisés ; un script qui tire des nombres aléatoires doit
   fixer sa graine, par exemple à partir de `parameters["seed"]`.
6. **`print` seulement pour déboguer.** Ce que le script affiche n'est conservé
   qu'en cas d'échec, dans son journal d'erreur.

## Quel Python exécute le script

Le script s'exécute dans un processus séparé, avec l'interpréteur Python
d'eqasim, dans le dossier eqasim (`model.root`) : pandas, numpy et geopandas
viennent de l'environnement d'eqasim, pas de celui d'eqasim-sa. La commande de
l'interpréteur est, par ordre de priorité :

1. `execution.python`, si vous l'écrivez, par exemple
   `python: [uv, run, --locked, python]` ou le chemin de l'interpréteur d'un
   environnement virtuel ;
2. sinon `execution.invocation` privée de son `-m synpp` final : l'invocation
   par défaut donne donc `uv run --locked python` ;
3. sans bloc `execution`, `uv run --locked python`.

Si `execution.invocation` ne se termine pas par `-m synpp` et
qu'`execution.python` est absent, le chargement d'une méta-configuration qui
déclare des analyses échoue avec `analyses need execution.python because
execution.invocation does not end with -m synpp`. La commande est résolue comme
l'invocation d'eqasim, y compris sous Windows. Le petit programme d'exécution
qu'eqasim-sa copie à côté de votre script tourne avec le Python d'eqasim, qui
doit être en version 3.9 ou plus récente. Le processus du script reçoit les
variables d'environnement d'eqasim-sa sauf `VIRTUAL_ENV`, qui désigne
l'environnement d'eqasim-sa lui-même : `uv run` dans le dossier eqasim utilise
alors l'environnement d'eqasim sans avertissement à ce sujet.

## Ce que fait `analyze`

`analyze` ne calcule que les analyses lues par au moins un indicateur. Pour
chacune, il copie le script une fois (modifier le script pendant `analyze`
n'a pas d'effet sur cette analyse), mesure une fois l'environnement Python,
puis, pour chaque run, réutilise le résultat en cache ou exécute le script sur
des copies des entrées archivées. Au plus `execution.workers` scripts
s'exécutent en même temps (1 par défaut).

Toute erreur d'un script fait échouer toute l'analyse, même avec
`completeness_threshold` inférieur à 1, avec un message qui nomme l'analyse, le
run et la dernière ligne de l'erreur :

```text
analysis 'mode_share' failed for run 0e599693c300002e9a1c0dffe06ff0ff: ValueError: trips has no mode column; set mode_choice: true in the eqasim configuration
```

La sortie d'erreur complète se trouve dans
`<campaign_root>/analyses-cache/logs/<analyse>-<runId>.log`. Corrigez le
script et relancez `analyze` : les résultats déjà calculés sont réutilisés.

`check`, y compris `check --smoke`, n'exécute pas les scripts d'analyse : ses
runs de smoke ignorent les indicateurs qui lisent une sortie d'analyse. Un
script défaillant n'apparaît qu'à `analyze` : essayez-le d'abord à la main.

## Essayer un script à la main

Dès qu'un run est archivé, appelez `compute` sur ses fichiers avec
l'interpréteur d'eqasim. Enregistrez ce fichier sous `try_commute.py` hors du
dossier du projet, par exemple dans le dossier qui contient le projet ou dans
un dossier temporaire : une relance d'`init` refuse tout fichier qu'elle n'a
pas créé (`foreign content in init project: try_commute.py`). Remplacez
ensuite les deux valeurs à compléter :

- `<campaign>` par la `campaign` de la méta-configuration, qui est aussi le nom
  du dossier sous `campaigns/` (par exemple `mon-analyse-sa-grid`) ;
- `<runId>` par un identifiant réel (un nom de dossier sous
  `campaigns/<campaign>/archives/`).

```python
import importlib.util

CAMPAIGN = "<campaign>"
RUN_ID = "<runId>"
archive = f"campaigns/{CAMPAIGN}/archives/{RUN_ID}"
spec = importlib.util.spec_from_file_location("commute", "configs/analyses/commute.py")
commute = importlib.util.module_from_spec(spec)
spec.loader.exec_module(commute)
print(
    commute.compute(
        {
            "trips": f"{archive}/trips.csv",
            "activities_spatial": f"{archive}/activities_spatial.gpkg",
            "persons": f"{archive}/persons.csv",
        },
        {"runId": RUN_ID, "seed": 1201, "factors": {"sampling_rate": 0.001}},
    )
)
```

La seed et les facteurs sont seulement transmis à `compute` ; `commute.py` les
ignore. Les chemins du script sont relatifs au dossier du projet : lancez-le
depuis le dossier du projet en remplaçant `<chemin vers eqasim>` par le dossier
eqasim, absolu ou relatif au dossier du projet (et non à `configs/`, dossier
auquel `model.root` est relatif : par exemple, `../../eqasim-france` dans un
fichier de départ devient `../eqasim-france` ici), et
`<chemin vers try_commute.py>` par le chemin du fichier, absolu ou relatif au
dossier du projet (`../try_commute.py` s'il est dans le dossier qui contient le
projet) :

```bash
uv run --project <chemin vers eqasim> --locked python <chemin vers try_commute.py>
```

Si uv affiche ``Project directory `...` does not exist``, le chemin est faux et
uv lance un autre Python, hors de l'environnement d'eqasim : le script échoue
alors avec une erreur trompeuse comme `No module named 'geopandas'`. Corrigez le
chemin.

Ici le script lit l'archive elle-même : il doit seulement la lire.

## Cache et recalcul

Chaque résultat est identifié par une empreinte : un SHA-256 (domaine
`eqasim-sa:analysis-result:v1`) du nom de l'analyse, du contenu du script (fins
de ligne normalisées), du SHA-256 de chaque fichier d'entrée, des paramètres du
run et de l'empreinte de l'environnement. L'empreinte de l'environnement est un
SHA-256 de la version de Python, des versions de pandas, numpy, pyarrow,
geopandas, shapely et pyogrio rapportées par l'interpréteur (`null` si une
bibliothèque est absente), et du SHA-256 de `uv.lock` dans le dossier
eqasim. Par conséquent :

- rien n'est recalculé si rien n'a changé ;
- modifier un script ne recalcule que les résultats de cette analyse ;
- de nouveaux runs ne calculent que leurs propres résultats ;
- une autre version de Python, pandas, numpy, pyarrow, geopandas, shapely ou
  pyogrio, ou un autre
  `uv.lock`, recalcule tous les résultats ; une modification du seul code
  d'eqasim, non (d'où la règle 1).

Les résultats sont gardés dans
`<campaign_root>/analyses-cache/<analyse>/<empreinte>.csv`, à côté de
`<empreinte>.sha256`, le SHA-256 du CSV. Une entrée du cache dont le contenu ne
correspond plus est supprimée et recalculée, et le champ `warnings` du résultat
d'`analyze` le signale :

```text
analysis cache entry commute/<fingerprint>.csv was altered; it was removed and recomputed
```

Le champ `analyses` du résultat d'`analyze` indique, pour chaque analyse,
combien de résultats de runs ont été calculés et combien ont été relus depuis
le cache. Après modification de `commute.py` dans l'exemple ci-dessus, sur une
campagne de quatre runs :

```json
"analyses": {"commute": {"computed": 4, "cached": 0}}
```

Relancer `analyze` sans aucun changement relit tous les résultats :
`"analyses": {"commute": {"computed": 0, "cached": 4}}`.

Le cache peut être supprimé à tout moment quand aucune commande ne tourne :
rien n'est perdu, `analyze` le recalcule.

## Après une campagne sur un supercalculateur

1. Planifiez et exécutez la campagne sur le supercalculateur, avec les sorties
   dont les futurs scripts pourraient avoir besoin (voir
   [Avant les runs](#avant-les-runs--archiver-assez)).
2. Rapatriez le dossier de campagne sur votre ordinateur (`scp`, `sftp`...).
   Le scratch et l'arborescence de données ne sont pas nécessaires.
3. Refaites le rendu des chemins machine de la méta-configuration pour votre
   ordinateur, écrivez les scripts et ajoutez `analyses` et les indicateurs
   qui les lisent.
4. Lancez `eqasim-sa analyze --meta-config configs/01-grid.yml`.

L'ordinateur a besoin : du dossier de campagne, de la méta-configuration, des
scripts, d'un fichier manifeste de données (le petit fichier JSON, pas les
données) et du dossier eqasim avec son environnement Python, qui exécute les
scripts (`uv run --locked` le crée à la première utilisation). Le code, la
configuration et le `uv.lock` d'eqasim peuvent différer de ceux des runs :
`analyze` vérifie la méta-configuration par rapport à ce que le plan a scellé
(`modelDocument` et contexte), pas par rapport au dossier eqasim courant.
`run`, en revanche, refuse de reprendre une campagne dès qu'eqasim a changé :
terminez les runs avant de modifier eqasim. Voir
[Analyser sur une autre machine](troubleshooting.md#analyser-sur-une-autre-machine).

## Ce qui est enregistré

L'analyse publiée enregistre, pour chaque sortie d'analyse et chaque run,
l'empreinte du résultat, le SHA-256 du script, l'empreinte de l'environnement,
le SHA-256 de chaque entrée, ainsi que le SHA-256 et la taille du résultat :

- dans `captured-run-set.json` (version de schéma 2), sous `analyses` dans
  chaque run ;
- dans `analysis-generation.json` (version 8), sous `provenance.analyses`, par
  analyse puis par `runId` ; les versions rapportées et le SHA-256 de `uv.lock`
  qui forment l'empreinte de l'environnement sont enregistrés une fois par
  analyse sous `provenance.analysisEnvironments` (champ additif : le manifeste
  reste en version 8).

La définition d'un indicateur qui lit une sortie d'analyse la décrit par
`{name, analysis, inputs, separator}` (domaine
`eqasim-sa:indicator-definition:v4`). Voir
[Workflow et résultats](workflow-and-results.md#scripts-danalyse).

## Limites

- `inputs` liste des sorties déclarées dans `outputs` dont le fichier est un
  `.csv` ou un `.gpkg` ; une analyse ne peut pas lire une autre analyse.
- Le `primary_indicator` d'une charte scientifique ne peut pas lire une sortie
  d'analyse : les indicateurs calculés par des scripts sont secondaires.
- Sans bloc `indicators`, les indicateurs par défaut ignorent les sorties
  d'analyse : listez les indicateurs voulus. Un bloc `indicators` déclaré
  remplace les indicateurs `tv_distance` par défaut : listez-les aussi pour
  les garder.
- Un nom d'analyse suit les règles des noms de sortie et doit différer de tout
  nom de sortie, sans tenir compte de la casse ; `logs` est réservé.
