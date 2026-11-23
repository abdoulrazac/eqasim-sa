# Documentation eqasim-sa

[English](../docs/index.md) | **Français**

Cette documentation décrit le comportement observé dans `src/` et `tests/`.
Le parseur Python est normatif : le projet ne fournit pas de JSON Schema.

## Documentation utilisateur

1. [Démarrage](getting-started.md) : installation, `init` et première campagne.
2. [Tutoriel des configurations structurées nommées](structured-configurations-tutorial.md) : comparer de bout en bout des listes `matching_attributes` ordonnées.
3. [Référence YAML](yaml-reference.md) : contrat complet d'une méta-configuration.
4. [Écrire un script d'analyse](analysis-scripts.md) : ajouter des grandeurs calculées à partir des fichiers archivés, même après les runs.
5. [Paramètres et méthodes](parameters-and-methods.md) : Grid, OAT discret, Morris canonique, Sobol et seeds.
6. [Workflow et résultats](workflow-and-results.md) : identités, stockage, reprise et figures.
7. [Référence CLI](cli-reference.md) : commandes, JSON et codes de sortie.
8. [Dépannage](troubleshooting.md) : erreurs fréquentes et remèdes sûrs.
9. [Glossaire](glossary.md) : vocabulaire précis du projet.
10. [Compatibilité de version](compatibility.md) : matrices des schémas, méthodes, statuts, refus et versions.

## Choisir une méthode

Si vous débutez, lisez d'abord le [cas pratique Grid](what-when-how-grid.md) : il
suit une question sur `sampling_rate` depuis la déclaration du paramètre et des
indicateurs jusqu'à l'interprétation des fichiers produits. Continuez avec OAT
discret pour les changements locaux, puis avec Morris canonique et Sobol
multi-scramble seulement si vous préparez une campagne scientifique déclarée.

| Guide | Question | Statut | Usage recommandé |
| --- | --- | --- | --- |
| [Grid](what-when-how-grid.md) | Que se passe-t-il pour chaque combinaison déclarée ? | Démonstration | Petites comparaisons lisibles et configurations structurées nommées |
| [OAT discret](what-when-how-discrete_oat.md) | Quels changements numériques locaux demandent de l'attention ? | Démonstration | Tri descriptif local pour préparer une étude ultérieure |
| [Morris canonique](what-when-how-canonical_morris.md) | Quels facteurs doivent rester à l'étude ? | Criblage scientifique | Pilote propre à l'étude, puis production séparée |
| [Sobol de démonstration](what-when-how-sobol.md) | La plomberie Sobol à un scramble fonctionne-t-elle ? | Démonstration non inférentielle | Exécuter le starter généré |
| [Sobol multi-scramble](what-when-how-multiscramble_sobol.md) | Que faut-il apprendre d'abord, ou que peut-on fixer ? | FP ou FF scientifique | Priorisation ou fixation des facteurs propre à l'étude |

## Documentation contributeur

- [Guide de développement](development-guide.md) : architecture, dépendances, tests et distribution.
- [Modèles générés](../src/eqasim_sa/templates/) : README et méta-configurations normatives de départ.

Retour à la [page d'accueil](../README-fr.md).

## Matrice rapide

| Besoin | Commande | Prérequis durable | Effet principal |
| --- | --- | --- | --- |
| Générer un projet | `init` | modèle, données | prépare les fichiers et hache les données, sans exécuter le pipeline |
| Sceller un design | `plan` | méta-config valide | `plan.parquet` immuable |
| Valider statiquement | `check` | plan; pour une cible, calibration référencée avec au moins un run complet | contrôles sans run cible |
| Tester réellement | `check --smoke` | conditions de `check` | deux runs temporaires réels |
| Chiffrer une cible | `estimate` | au moins un run complet de calibration | devis JSON temps/disque/scratch |
| Exécuter | `run` | plan scellé, bloc `execution` | archives et marqueurs manquants |
| Recalculer l'analyse | `analyze` | complétude suffisante | Parquet d'indicateurs et de sensibilité |
| Dessiner les figures | `plot` | analyse publiée (`analyze` faite), méta-config de la campagne et plan scellé | figures PNG valides pour la méthode, plus `figures.json`, sous `figures/<generationId>/` |

## Méthodes

| Méthode | Rôle de la seed | Positions | Sortie spécifique |
| --- | --- | --- | --- |
| Grid | réplication | produit des tailles de domaines × seeds | `replication-dispersion.parquet` |
| OAT discret | réplication | `T(D+1)R` | `discrete-oat-effects.parquet` |
| Morris canonique | réplication dérivée par bloc de trajectoire | `T(D+1)R` | Parquet de classement, convergence et inversions de rang |
| Sobol | facteur | `N(2D+2)` | `sobol-indices.parquet` |
| Sobol multi-scramble | facteur | `S*N(2D+2)` | quatre fichiers `multiscramble-sobol-*.parquet` |

`D` inclut la seed pour Sobol. Le nombre de runs uniques peut être inférieur au
nombre de positions, car plusieurs positions statistiques peuvent désigner la
même configuration résolue.

`init` génère des starters de démonstration Grid, OAT discret et Sobol. Il ne
génère ni starter Sobol multi-scramble, dont la charte FP/FF et les seeds de
scrambles doivent être propres à l'étude, ni Morris canonique, dont le contrat de criblage scientifique doit être
propre à l'étude.

`init` prépare les fichiers et les empreintes. Seul le `run` de calibration
fournit les observations de ressources utilisées par `check` et `estimate`.

## Règles à retenir

- Exécuter la calibration avant `check` ou `estimate` d'une cible qui la référence.
- Ne jamais modifier `plan.parquet` ni `runs/<runId>.parquet` à la main.
- Ne pas modifier le code ni la configuration d'eqasim pendant une campagne : ils font partie de son identité. Une fois les runs terminés, `analyze` fonctionne toujours après de telles modifications.
- Déclarer sous `outputs`, avant de planifier, tous les fichiers que vous voudrez peut-être analyser plus tard : les scripts d'analyse ne lisent que des fichiers archivés.
- Ajouter `analysis.synthesis.population` à `run:` dans votre configuration eqasim avant d'utiliser les sorties `age`, `distance` ou `nbr_vehicle`.
- Utiliser un nouveau nom `campaign` (donc un nouveau dossier de campagne) lorsque le contexte ou le design change.
- Considérer le scratch comme jetable, jamais comme preuve de complétude.
