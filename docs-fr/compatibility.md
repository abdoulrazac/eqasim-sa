# Compatibilite de version

[English](../docs/compatibility.md) | **Francais**

Cette page decrit `eqasim-sa` 0.3.0. Le parseur Python et le code d'analyse sont
normatifs. La compatibilite signifie que la provenance historique reste lisible ;
elle ne transforme pas une preuve historique en campagne scientifique et ne la
migre pas.

## Matrice des plans et methodes

| Methode | Charte acceptee | Objectif et contrat stochastique | Schema du plan | Metadonnees de methode | Sens dans cette version |
| --- | --- | --- | --- | --- | --- |
| `grid` | demonstration, y compris une ancienne charte omise | aucun ; la seed est une replication | v1 | aucune | demonstration uniquement |
| `morris` | demonstration uniquement | OAT a niveaux adjacents ; la seed est une replication | v1 | `morrisDesignVersion` v1 | provenance historique de demonstration |
| `discrete_oat` | demonstration uniquement | effets locaux normalises entre niveaux adjacents ; la seed est une replication | v1 | `discreteOatDesignVersion` v1 | criblage de demonstration ; pas Morris canonique |
| `sobol` | demonstration, y compris une ancienne charte omise | un scramble et bootstrap de lignes ; la seed est un facteur | v1 | `sobolDesignVersion` v1 | preuve historique lisible mais non inferentielle |
| `canonical_morris` | scientifique uniquement | `screening` ; `conditional_mean` ; CRN dans la trajectoire ; allocation pilote ou production declaree | v2 | `canonicalMorrisDesignVersion` v1 | criblage scientifique soumis au budget, a l'arret et a la robustesse |
| `multiscramble_sobol` | scientifique uniquement | `fp` ou `ff` ; `finite_seed_factor` ; dependance independante ; au moins quatre scrambles independants explicites | v2 | `multiScrambleSobolDesignVersion` v1 | FP/FF scientifique soumis au budget, a l'arret et a la robustesse |

Les deux familles de plans utilisent `canonicalizationVersion` v1. Les analyseurs
scientifiques `canonical_morris` et `multiscramble_sobol` reconstruisent
strictement les metadonnees scellees du plan, son contexte et son design. Les
analyseurs des methodes historiques valident leurs contrats propres lorsque
cette validation est implementee. L'analyse Grid s'appuie plutot sur la methode,
l'identite des marqueurs de run attendus et leur completude ; elle ne reconstruit
pas toutes les metadonnees du plan et n'en fournit aucune validation scientifique.
Il n'existe aucune migration semantique ou en place entre les versions de plan.

## Matrice d'analyse et de revendication

Chaque analyse ecrit `analysis-generation.json` version 6 et
`governed-report.json` version 1.

| Etat | `inferenceStatus` | Preuve de convergence | Comportement de revendication |
| --- | --- | --- | --- |
| Grid, Morris historique ou OAT discret | `demonstration_noninferential` | absente | `claimAuthorized: false` |
| Sobol historique a un scramble | `legacy_noninferential` | absente | `claimAuthorized: false` |
| Pilote Morris canonique | `pilot_only` | publiee, sans promouvoir le pilote | `claimAuthorized: false` |
| Production Morris canonique | `conditional` si tous les criteres d'arret passent, sinon `unstable` | requise et evaluee | robustesse en attente ; `claimAuthorized: false` |
| Sobol multi-scramble FP/FF | `conditional` si tous les criteres d'arret passent, sinon `unstable` | requise et evaluee entre scrambles independants | robustesse en attente ; `claimAuthorized: false` |

Le criblage conserve les facteurs a examiner ; il n'estime pas des parts de
variance et ne justifie pas leur fixation. FP indique quelle incertitude apprendre
plus precisement en premier. FF exige une tolerance pratique declaree sur la part
de variance d'ordre total, interactions incluses. La seed finie est non
actionnable et exclue des rangs de decision FP/FF.

## Matrice des refus

| Etat incompatible | Refus |
| --- | --- |
| `morris`, `discrete_oat` ou `sobol` avec une charte scientifique | rejete ; ces methodes exigent `charter.status: demonstration` |
| `canonical_morris` ou `multiscramble_sobol` sans charte scientifique | rejete ; ces methodes exigent `charter.status: scientific` |
| Morris canonique avec un objectif autre que `screening` | rejete |
| Morris canonique sans `conditional_mean` ou sans dependance independante | rejete |
| Sobol multi-scramble sans objectif `fp`/`ff`, `finite_seed_factor` ou dependance independante | rejete |
| Tolerance FF hors de l'intervalle sans dimension `[0,1]` | rejetee |
| Metadonnees scellees d'un plan scientifique absentes, non prises en charge, non canoniques, alterees ou incompatibles avec le contexte | rejete avant publication par les analyseurs Morris canonique et Sobol multi-scramble |
| Plan Grid presente a l'analyse | controle par la methode, l'identite des marqueurs de run attendus et leur completude ; aucune revendication de validation scientifique des metadonnees |
| Positions scientifiques au-dessus du budget declare apres reserve | rejete avant publication du plan |
| Analyse OAT ou Sobol incomplete | rejetee car la completude doit valoir `1.0` |
| Preuve d'arret non prise en charge ou non evaluable | echec ferme, donc non convergee |

## Frontieres de init et Nantes

`eqasim-sa init` genere seulement des starters de demonstration pour calibration,
Grid, OAT discret et Sobol historique a un scramble. Il ne genere ni starter
scientifique Morris canonique ou Sobol multi-scramble, ni protocole Nantes, ni
plan, ni sortie de simulation ou d'analyse.

`protocols/nantes-scientific/` est un protocole du depot, pas une donnee du
paquet ni un modele de `init`. Son renderer ne produit une meta-configuration de
phase qu'apres verification des plans et preuves amont scelles. Il refuse sans
creer de sortie les preuves absentes, liees symboliquement, mal formees,
alterees, incompatibles avec le contexte ou la phase. Les tests du renderer
emploient de faux modeles et preuves temporaires. Ils n'etablissent pas que le
protocole Nantes reel a ete planifie ou execute.

## Matrice de version

| Version | Python | Schemas de plan lus/ecrits | Manifeste d'analyse | Migration |
| --- | --- | --- | --- | --- |
| 0.3.0 | `>=3.13,<3.14` | v1 pour les methodes de demonstration/historiques ; v2 pour les methodes scientifiques actuelles, avec chaque famille de metadonnees v2 en v1 | v6 | aucune |

Voir les [preuves de version](../docs/release-evidence-0.3.0.md) pour les
commandes reellement executees, les empreintes des artefacts et les limites de
l'environnement.
