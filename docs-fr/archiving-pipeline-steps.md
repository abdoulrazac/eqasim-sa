# Archiver la sortie d'une autre étape du pipeline

[English](../docs/archiving-pipeline-steps.md) | **Français**

eqasim-sa archive les fichiers qu'eqasim écrit dans son dossier de sortie
(`output_path`) et que vous déclarez sous [`outputs`](yaml-reference.md#outputs).
La plupart des étapes du pipeline eqasim (étapes synpp) n'y écrivent rien :
`synthesis.population.enriched`, par exemple, renvoie la population enrichie
aux étapes qui l'utilisent, mais aucun fichier du dossier de sortie ne la
contient. Pour conserver le résultat d'une telle étape à chaque run, ajoutez
une petite étape à vous qui l'écrit dans `output_path`, faites-la exécuter et
déclarez son fichier. eqasim-sa n'a besoin de rien d'autre : il ne connaît pas
les noms d'étapes et ne lit jamais le cache synpp.

## Sommaire

- [Où se trouve la sortie d'une étape](#où-se-trouve-la-sortie-dune-étape)
- [Écrire une étape d'export](#écrire-une-étape-dexport)
- [La faire exécuter](#la-faire-exécuter)
- [Déclarer son fichier](#déclarer-son-fichier)
- [Conséquences](#conséquences)

## Où se trouve la sortie d'une étape

Une étape synpp a deux sortes de résultats :

- la valeur renvoyée par sa fonction `execute(context)` (pour
  `synthesis.population.enriched`, une table pandas des personnes), que synpp
  enregistre comme fichier pickle dans son dossier de cache
  (`working_directory`) et transmet aux étapes qui la déclarent avec
  `context.stage(...)` ;
- les fichiers qu'elle écrit dans son propre dossier de cache
  (`context.path()`), pour les étapes qui s'en servent.

Seules des étapes comme `synthesis.output` et `analysis.synthesis.population`
copient des résultats dans `output_path`. eqasim-sa ne lit pas le cache à la
place : ses noms de fichiers contiennent des empreintes calculées par synpp à
partir de la configuration et du code de l'étape, relire un fichier pickle
dépend des versions de Python et des bibliothèques qui l'ont écrit, et une
campagne exécutée sur un cluster ne rapatrie que les archives des runs, pas le
cache.

## Écrire une étape d'export

Une étape d'export est une étape synpp ordinaire : un module Python avec une
fonction `configure(context)` qui déclare ce dont elle a besoin et une fonction
`execute(context)` qui écrit le fichier. Celle-ci écrit la population enrichie
dans un fichier Parquet. Enregistrez-la sous `exports/enriched_population.py`
dans le dossier eqasim (`model.root`), à côté de `synthesis/` et `data/` :

```python
import os


def configure(context):
    context.stage("synthesis.population.enriched")
    context.config("output_path")
    context.config("output_prefix", "ile_de_france_")


def execute(context):
    df_population = context.stage("synthesis.population.enriched")

    output_path = context.config("output_path")
    output_prefix = context.config("output_prefix")

    df_population.to_parquet(
        os.path.join(output_path, output_prefix + "enriched_population.parquet")
    )
```

- `exports` est un simple dossier : comme les dossiers `synthesis/` et `data/`
  d'eqasim, il n'a pas besoin de `__init__.py`. eqasim-sa lance synpp depuis le
  dossier eqasim (`uv run --locked python -m synpp` par défaut), et synpp
  importe l'étape `exports.enriched_population` depuis ce dossier. Tout autre
  nom de dossier ou de module convient de la même façon, tant qu'il ne
  coïncide pas avec un nom d'eqasim.
- `context.stage(...)` nomme l'étape dont vous voulez le résultat ; remplacez
  `synthesis.population.enriched` par toute autre étape qui renvoie une table,
  et changez le nom du fichier en conséquence.
- `ile_de_france_` est la valeur par défaut d'`output_prefix` dans
  `synthesis/output.py` d'eqasim ; avec la même valeur par défaut, votre
  fichier reçoit le même préfixe que les fichiers d'eqasim, et `{prefix}` dans
  eqasim-sa désigne les deux.
- Déclarer `output_path` avec `context.config(...)` est indispensable, pas
  seulement pour le lire : synpp réutilise une étape en cache quand sa
  configuration n'a pas changé, et chaque run d'eqasim-sa a son propre
  `output_path` ; l'étape réécrit donc son fichier à chaque run.

Pour un fichier CSV, remplacez la dernière instruction par :

```python
    df_population.to_csv(
        os.path.join(output_path, output_prefix + "enriched_population.csv"),
        sep=";", index=False,
    )
```

## La faire exécuter

Ajoutez l'étape à `run:` de votre copie de la configuration eqasim
(`model.config`) :

```yaml
run:
  - synthesis.output
  - analysis.synthesis.population
  - exports.enriched_population
```

eqasim-sa exécute cette liste telle quelle (voir
[Ce qu'eqasim exécute](yaml-reference.md#ce-queqasim-exécute)). synpp exécute
d'abord l'étape `synthesis.population.enriched` si nécessaire ; quand une autre
étape de la liste en a déjà besoin, synpp réutilise son résultat.

## Déclarer son fichier

Déclarez le fichier sous `outputs` avec `file`, obligatoire pour un nom
qu'eqasim-sa ne connaît pas :

```yaml
outputs:
  enriched_population:
    file: "{prefix}enriched_population.parquet"
```

Chaque run l'archive alors sous le nom `enriched_population.parquet`. Un
[script d'analyse](analysis-scripts.md) peut le lire avec
`pandas.read_parquet`. Les indicateurs (`tv_distance`, `share`, `value`) ne
lisent que des fichiers CSV : calculez la grandeur voulue avec un script
d'analyse, dont les indicateurs lisent le résultat, ou écrivez le fichier en
CSV et déclarez son séparateur :

```yaml
outputs:
  enriched_population:
    file: "{prefix}enriched_population.csv"
    separator: ";"
```

## Conséquences

- Le fichier de l'étape fait partie de
  l'[empreinte du code d'eqasim](yaml-reference.md#lempreinte-du-code-deqasim),
  comme tout fichier `*.py` sous `model.root`, et la configuration, avec sa
  liste `run:`, fait partie de l'identité de la campagne. Ajoutez les deux
  avant `plan` : les modifier ensuite change l'identité de la campagne, et
  chaque run d'une campagne planifiée refuse de démarrer quand le code a
  changé.
- Un fichier déclaré que le run ne produit pas (l'étape manque dans `run:`, ou
  écrit un autre nom) fait échouer le run avec
  `eqasim did not write declared output 'enriched_population' (expected
  nantes_enriched_population.parquet); check that the stage producing it is
  in run: of your eqasim config`. `check --smoke` exécute deux vrais runs et
  le détecte donc avant la campagne.
- Le chargement ne vérifie pas que l'étape est dans `run:` (cette vérification
  ne concerne que les sorties connues laissées à leur fichier par défaut), et
  `show-config` affiche la déclaration avec le nom de fichier résolu en
  commentaire :

  ```yaml
  outputs:
    enriched_population:
      file: '{prefix}enriched_population.parquet'  # nantes_enriched_population.parquet
  ```

- Chaque fichier déclaré est copié dans l'archive de chaque run : une table de
  personnes grossit avec `sampling_rate`.
