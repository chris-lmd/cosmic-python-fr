"""
Configuration partagée pour les tests.

Le mapping ORM est démarré une seule fois pour toute la session de tests.
Cela permet aux tests d'intégration et e2e d'utiliser SQLAlchemy
sans interférer avec les tests unitaires.
"""

import pytest

from allocation.adapters import orm


@pytest.fixture(scope="session", autouse=True)
def mappers():
    """Démarre le mapping ORM une fois pour toute la session."""
    orm.start_mappers()
