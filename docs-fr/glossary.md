# Glossaire

[English](../docs/glossary.md) | **Français**

| Terme | Définition |
| --- | --- |
| **Méta-configuration** | L'unique fichier YAML qui décrit une campagne ; les clés omises prennent des valeurs par défaut, et `show-config` affiche le fichier complet résolu. |
| **Campagne** | Ensemble durable décrit par une méta-configuration et stocké sous son dossier `campaign_root`, par défaut `../campaigns/<campaign>`. |
| **Cible** | Campagne dont on veut exécuter et analyser le design, par opposition à la calibration des ressources. Une cible générée reste une démonstration sans contrat scientifique satisfait. |
| **Campagne de démonstration** | Petite campagne qui valide configuration, exécution, stockage et plomberie d'analyse ; elle ne soutient aucune revendication inférentielle ou décisionnelle. |
| **Campagne scientifique** | Campagne avec contexte décisionnel, modèle d'incertitude, estimand, budget, critères de convergence et contrôles de robustesse déclarés. |
| **Criblage** | Identifie les facteurs à conserver à l'étude ; il n'estime pas des parts de variance et ne prouve pas qu'un facteur peut être fixé. |
| **Priorisation des facteurs (FP)** | Identifie le facteur à apprendre plus précisément en premier pour réduire l'incertitude de sortie espérée. |
| **Fixation des facteurs (FF)** | Identifie un facteur fixable dans une tolérance pratique déclarée, interactions comprises. |
| **OAT discret** | `method: discrete_oat` ; niveaux numériques adjacents et effets normalisés locaux. C'est une démonstration, pas Morris canonique. |
| **Morris canonique** | `method: canonical_morris` ; trajectoires dans l'espace de probabilité unitaire, Delta commun et transformations déclarées. Uniquement pour le criblage scientifique. |
| **Scramble QMC** | Une séquence Sobol à faible discrépance randomisée indépendamment. L'incertitude exige plusieurs scrambles indépendants, pas un bootstrap des lignes d'un seul scramble. |
| **Sobol multi-scramble** | `method: multiscramble_sobol` ; analyse scientifique FP/FF avec scrambles QMC indépendants et incertitude de Student entre scrambles. |
| **Moyenne conditionnelle** | Cible Morris canonique `E_seed[Y|X]`, estimée avec au moins deux réplications de seed et des CRN dans chaque trajectoire. |
| **Variance intrinsèque / plancher de bruit** | Variance ponctuelle non biaisée entre réplications, en unités de l'indicateur au carré; elle reste descriptive et ne remplace pas l'incertitude entre trajectoires. |
| **Allocation pilote** | Campagne canonique immuable qui estime un besoin de réplications et recommande une campagne/racine de production séparée. |
| **Nuisance seed finie** | Support fini du facteur `seed` en Sobol multi-scramble, déclaré dans `experiment.seeds` avec `experiment.seeds_provenance`. Ses indices sont diagnostiques, non actionnables et n'identifient pas le bruit intrinsèque sans restriction. |
| **Calibration des ressources** | Campagne préalable qui mesure durée, octets durables et cache worker; elle ne règle pas les paramètres scientifiques. |
| **Design / plan d'expérience** | Règles de génération des positions Grid, de trajectoire ou Sobol. Le fichier scellé est `plan.parquet`. |
| **Position de design** | Ligne statistique du plan. Plusieurs positions peuvent désigner le même run. |
| **`designId`** | Identifiant stable de 32 caractères hexadécimaux d'une position de design. |
| **Run** | Exécution unique d'une configuration eqasim résolue avec une seed. |
| **`runId`** | Identifiant stable de 32 caractères hexadécimaux de la configuration canonique et de la seed. |
| **Seed (graine)** | Entier injecté comme `random_seed`, déclaré dans `experiment.seeds` ; selon la méthode, elle représente une réplication ou un facteur Sobol nommé `seed`. |
| **Worker** | Slot local ou SLURM qui traite séquentiellement un lot de runs et possède son cache exclusif. |
| **Option machine** | Valeur d'infrastructure sous `execution.machine_options`, appliquée seulement à l'exécution, hors design et hors `runId`. |
| **Sortie** | Table écrite par eqasim pour chaque run (un fichier Parquet), déclarée sous `outputs` avec sa colonne de classes (`category`), sa colonne simulée (`simulated`) et ses colonnes de référence (`references`). |
| **Indicateur** | Nombre calculé à partir d'une sortie d'un run, que l'analyse étudie ; `tv_distance`, la distance de variation totale entre distributions simulée et de référence, est l'indicateur intégré. |
| **Backend** | Mécanisme d'exécution des lots, `local` ou `slurm`. |
| **`contextHash`** | SHA-256 domain-separated du commit modèle, du manifeste, du lockfile et de la version d'`eqasim-sa`. |
| **Manifeste** | `manifest.json`, inventaire trié des fichiers de données avec tailles et SHA-256, produit par `init`. |
| **Marqueur** | `runs/<runId>.parquet`, publié en dernier et seule preuve de complétude reconnue. |
| **Archive** | Répertoire `archives/<runId>/` contenant les sorties Parquet durables du run. |
| **Scratch** | Stockage temporaire des caches et tentatives workers; il est jetable et ne prouve aucune complétude. |

Voir [Workflow et résultats](workflow-and-results.md) pour les formats et
[Paramètres et méthodes](parameters-and-methods.md) pour leur rôle scientifique.
