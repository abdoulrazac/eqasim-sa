# Contribuer à eqasim-sa

[English](CONTRIBUTING.md)

Merci de contribuer à `eqasim-sa`. Avant une modification importante, décrivez
le problème et le comportement proposé dans une issue ou une discussion afin de
valider le périmètre et les implications scientifiques.

En soumettant une contribution, vous acceptez sa distribution sous la [licence
publique générale GNU version 2.0 uniquement](LICENSE) du projet.

## Environnement de développement

Le projet cible Python 3.13. Installez l'environnement verrouillé depuis la
racine du dépôt :

```bash
uv sync --extra test --locked
```

N'ajoutez pas aux commits les fichiers générés, environnements virtuels,
identifiants, jeux de données locaux ou résultats de campagne. N'ajoutez aucune
donnée eqasim réelle ni configuration contenant des chemins privés ou des
secrets.

## Réaliser une modification

- Gardez un périmètre ciblé et ajoutez des tests de régression pour tout
  changement de comportement.
- Préservez le déterminisme des plans, l'immutabilité des archives de run et des
  générations d'analyse publiées, et la validation stricte aux frontières de
  confiance.
- Traitez toute modification des identités de plan, schémas, designs
  stochastiques, estimands ou autorisations de revendication comme un changement
  de compatibilité. Documentez-la dans `docs/` et `docs-fr/`, puis ajoutez une
  entrée dans `CHANGELOG.md`.
- Maintenez les documentations utilisateur française et anglaise cohérentes
  lorsque le comportement ou la terminologie change.

Le [guide de développement](docs-fr/development-guide.md) décrit plus en détail
l'architecture, la stratégie de test, le contenu du paquet et les invariants du
projet.

## Validation

Exécutez les contrôles adaptés à votre modification : au moins les tests et le
contrôle statique ci-dessous (imports et variables inutilisés, redéfinitions).
La suite de tests dure quelques minutes. Elle est testée sous Linux
(Python 3.13 : 1558 réussis, 4 ignorés) ; le flux d'intégration continue
GitHub Actions `.github/workflows/tests.yml` l'exécute sous Ubuntu, macOS et
Windows, et aucun résultat Windows ou macOS n'est revendiqué tant que
l'intégration continue n'a pas tourné.

```bash
uv run --locked --extra test pytest
uvx ruff check src tests protocols --select F401,F841,F811 --no-cache
```

Avant de proposer une version, exécutez au minimum la validation complète :

```bash
uv lock --check
uv run --locked --extra test pytest
uvx ruff check src tests protocols --select F401,F841,F811 --no-cache
git diff --check
uv build
uv run --locked python .github/scripts/verify_release_wheel.py \
  dist/eqasim_sa-0.4.0-py3-none-any.whl
```

Inspectez les archives nouvellement construites. Les fichiers préexistants dans
`build/` ou `dist/` ne prouvent rien sur les sources courantes.

Les versions sont publiées uniquement sur GitHub. Pousser une étiquette
`vX.Y.Z` (égale à la `version` de `pyproject.toml`) lance
`.github/workflows/release.yml`, qui vérifie le fichier de verrouillage, exécute
les tests, construit la roue (wheel) et l'archive source (sdist), exécute le
contrôle de la roue ci-dessus, exporte les versions d'exécution de `uv.lock`
dans `constraints.txt` et crée la version GitHub avec les deux archives et
`constraints.txt` joints.

## Proposer la modification

Décrivez le comportement visible, l'impact sur la compatibilité, les hypothèses
scientifiques et les validations réalisées. Ajoutez un petit exemple
reproductible si la modification touche la CLI ou une configuration de campagne.
Ne publiez jamais d'identifiant, de donnée privée ni de chemin propre à votre
machine dans une issue, un patch, une fixture de test ou un journal.
