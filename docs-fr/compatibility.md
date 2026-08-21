# Compatibilite de version

[English](../docs/compatibility.md) | **Francais**

Cette page decrit `eqasim-sa` 0.3.0 ainsi que le contrat non publie courant des
facteurs structures. Le parseur Python et le code d'analyse sont normatifs. La
compatibilite signifie que la provenance historique reste lisible ; elle ne
transforme pas une preuve historique en campagne scientifique et ne la migre pas.

## Matrice des plans et methodes

| Methode | Charte acceptee | Objectif et contrat stochastique | Schema du plan | Metadonnees de methode | Sens dans cette version |
| --- | --- | --- | --- | --- | --- |
| `grid` | demonstration, y compris une ancienne charte omise | aucun ; la seed est une replication | v1 ; v3 avec facteur structure | aucune | demonstration uniquement ; premier choix recommande pour une structure nommee |
| `morris` | demonstration uniquement | OAT a niveaux adjacents ; la seed est une replication | v1 | `morrisDesignVersion` v1 | provenance historique de demonstration |
| `discrete_oat` | demonstration uniquement | effets locaux normalises entre niveaux adjacents ; la seed est une replication | v1 | `discreteOatDesignVersion` v1 | criblage de demonstration ; pas Morris canonique |
| `sobol` | demonstration, y compris une ancienne charte omise | un scramble et bootstrap de lignes ; la seed est un facteur | v1 ; v3 avec facteur structure | `sobolDesignVersion` v1 | preuve historique lisible mais non inferentielle |
| `canonical_morris` | scientifique uniquement | `screening` ; `conditional_mean` ; CRN dans la trajectoire ; allocation pilote ou production declaree | v2 | `canonicalMorrisDesignVersion` v1 | criblage scientifique soumis au budget, a l'arret et a la robustesse |
| `multiscramble_sobol` | scientifique uniquement | `fp` ou `ff` ; `finite_seed_factor` ; dependance independante ; au moins quatre scrambles independants explicites | v2 ; v3 avec facteur structure | `multiScrambleSobolDesignVersion` v1 | FP/FF scientifique soumis a une distribution de labels defendable, au budget, a l'arret et a la robustesse |

Tous les plans utilisent `canonicalizationVersion` v1. Le schema v3 est ecrit
uniquement pour un plan structure et ajoute la paire canonique exacte
`structuredFactorsVersion: 1` et `structuredFactors`. Sous le schema de plan v3,
Sobol historique et Sobol multi-scramble recopient aussi le mapping complet
label-vers-payload dans leurs metadonnees de methode v1 comme second invariant
scelle ; les deux mappings canoniques doivent etre exactement egaux. Cette regle
est additive sous v3 : les versions de metadonnees de methode restent v1 et les
octets des plans non structures restent inchanges. Les analyseurs
scientifiques `canonical_morris` et `multiscramble_sobol` reconstruisent
strictement les metadonnees scellees du plan, son contexte et son design. Les
analyseurs des methodes historiques valident leurs contrats propres lorsque
cette validation est implementee. L'analyse Grid directe valide la forme et les
labels v3, mais ne dispose d'aucune metadonnee de methode ni configuration modele
pour lier les payloads. Le preflight applicatif CLI fait autorite pour cette liaison
complete : il reconstruit tout plan structure depuis la meta-configuration avant
l'analyse coeur.
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
| `seed` structuree, alias de payload identique, liste ordonnee mal formee ou cle modele qui n'est pas une liste de chaines | rejete |
| Facteur structure avec `morris`, `discrete_oat` ou `canonical_morris` | rejete car l'ordre nominal des labels est arbitraire |
| Metadonnees scellees d'un plan scientifique absentes, non prises en charge, non canoniques, alterees ou incompatibles avec le contexte | rejete avant publication par les analyseurs Morris canonique et Sobol multi-scramble |
| Plan Grid presente a l'analyse | l'analyse coeur directe valide methode, marqueurs, completude, forme et labels v3 ; le preflight applicatif reconstruit le plan pour lier les payloads au modele et a la meta-configuration |
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
| 0.3.0 + Unreleased | `>=3.13,<3.14` | plans v1/v2 existants ; v3 uniquement pour les plans structures avec metadonnees structurees v1 | v6 | aucune |

Voir les [preuves de version](../docs/release-evidence-0.3.0.md) pour les
commandes reellement executees, les empreintes des artefacts et les limites de
l'environnement.
