# Glossaire

[English](../docs/glossary.md) | **Français**

| Terme | Définition |
| --- | --- |
| **Campagne** | Ensemble durable décrit par une méta-configuration et stocké sous un `campaign_root`. |
| **Cible** | Campagne scientifique Grid, Morris ou Sobol dont on veut exécuter et analyser le design, par opposition à la calibration des ressources. |
| **Calibration des ressources** | Campagne préalable qui mesure durée, octets durables et cache worker; elle ne règle pas les paramètres scientifiques. |
| **Design / plan d'expérience** | Règles de génération des positions Grid, Morris ou Sobol. Le fichier scellé est `plan.parquet`. |
| **Position de design** | Ligne statistique du plan. Plusieurs positions peuvent désigner le même run. |
| **`designId`** | Identifiant stable de 32 caractères hexadécimaux d'une position de design. |
| **Run** | Exécution unique d'une configuration eqasim résolue avec une seed. |
| **`runId`** | Identifiant stable de 32 caractères hexadécimaux de la configuration canonique et de la seed. |
| **Seed** | Entier injecté comme `random_seed`; selon la méthode, elle représente une réplication ou un facteur Sobol. |
| **Worker** | Slot local ou SLURM qui traite séquentiellement un lot de runs et possède son cache exclusif. |
| **Override** | Valeur d'infrastructure appliquée seulement à l'exécution, hors design et hors `runId`. |
| **Scalariseur** | Fonction qui réduit un artefact en un indicateur scalaire; `tv_distance` est le scalariseur intégré. |
| **Backend** | Mécanisme d'exécution des lots, `local` ou `slurm`. |
| **`contextHash`** | SHA-256 domain-separated du commit modèle, du manifeste, du lockfile et de la version d'`eqasim-sa`. |
| **Manifeste** | `manifest.json`, inventaire trié des fichiers de données avec tailles et SHA-256, produit par `init`. |
| **Marqueur** | `runs/<runId>.parquet`, publié en dernier et seule preuve de complétude reconnue. |
| **Archive** | Répertoire `archives/<runId>/` contenant les artefacts Parquet durables du run. |
| **Scratch** | Stockage temporaire des caches et tentatives workers; il est jetable et ne prouve aucune complétude. |

Voir [Workflow et résultats](workflow-and-results.md) pour les formats et
[Paramètres et méthodes](parameters-and-methods.md) pour leur rôle scientifique.
