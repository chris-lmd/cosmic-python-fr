# Chapitre 0 -- Mise en route

## Pré-requis

Avant de commencer, assurez-vous d'avoir installé :

| Outil | Version | Vérification |
|-------|---------|--------------|
| **Python** | 3.12+ | `python --version` |
| **git** | récent | `git --version` |
| **uv** (recommandé) ou **pip** | récent | `uv --version` ou `pip --version` |

## Installation

```bash
# 1. Cloner le dépôt
git clone https://github.com/chris-lmd/cosmic-python-fr.git cosmic-python-fr
cd cosmic-python-fr

# 2. Installer les dépendances
# Option A : avec uv (recommandé)
uv sync

# Option B : avec pip
pip install -e ".[dev]"
```

## Vérification

Lancez la suite de tests pour vérifier que tout fonctionne :

```bash
pytest
```

Vous devriez voir **33 tests qui passent**. Les tests sont organisés avec trois marqueurs :

| Marqueur | Description | Commande |
|----------|-------------|----------|
| `unit` | Tests unitaires, rapides, sans I/O | `pytest -m unit` |
| `integration` | Tests d'intégration (base de données) | `pytest -m integration` |
| `e2e` | Tests end-to-end (API complète) | `pytest -m e2e` |

## Documentation locale

Pour consulter ce guide en local avec un rendu complet :

```bash
# Installer les dépendances de documentation
pip install -e ".[docs]"

# Lancer le serveur de documentation
mkdocs serve
```

Puis ouvrez [http://127.0.0.1:8000](http://127.0.0.1:8000) dans votre navigateur.

## Structure du projet

```
cosmic-python-fr/
├── src/allocation/
│   ├── adapters/            # Couche infrastructure
│   │   ├── orm.py           # Mapping SQLAlchemy
│   │   ├── repository.py    # Implémentation du Repository
│   │   └── notifications.py # Adapter de notifications
│   ├── domain/              # Couche domaine (code pur)
│   │   ├── model.py         # Entités, Value Objects, Agrégats
│   │   ├── commands.py      # Messages de type Command
│   │   └── events.py        # Messages de type Event
│   ├── entrypoints/         # Points d'entrée
│   │   └── flask_app.py     # API REST Flask
│   ├── service_layer/       # Couche service
│   │   ├── handlers.py      # Handlers (orchestration)
│   │   ├── messagebus.py    # Message Bus
│   │   ├── unit_of_work.py  # Unit of Work
│   │   └── bootstrap.py     # Composition Root
│   └── views/               # Lecture (CQRS)
│       └── views.py         # Requêtes en lecture
├── tests/
│   ├── unit/                # Tests unitaires (fakes, pas d'I/O)
│   ├── integration/         # Tests d'intégration (BDD réelle)
│   └── e2e/                 # Tests end-to-end (API HTTP)
├── docs/                    # Ce guide (MkDocs)
└── pyproject.toml           # Configuration du projet
```

## Comment lire ce guide

Le guide est organisé en **deux parties** qui se lisent dans l'ordre :

**Partie 1 (Chapitres 1-7)** introduit les fondations : modèle de domaine, Repository, Service Layer, Unit of Work, et Agrégats. Chaque chapitre ajoute un pattern et montre comment il résout un problème concret.

**Partie 2 (Chapitres 8-13)** construit l'architecture événementielle : Domain Events, Message Bus, Commands, events externes, CQRS et injection de dépendances.

!!! tip "Conseils de lecture"

    - **Lisez dans l'ordre.** Chaque chapitre s'appuie sur le précédent.
    - **Regardez le code.** Les fichiers dans `src/allocation/` correspondent exactement à l'état final décrit dans le guide.
    - **Lancez les tests.** Après chaque chapitre, identifiez les tests correspondants dans `tests/` et exécutez-les pour vérifier votre compréhension.
    - **L'épilogue** résume les compromis de chaque pattern et donne des conseils pragmatiques.
