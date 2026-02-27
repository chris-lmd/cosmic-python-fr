"""
Pattern Unit of Work.

Le Unit of Work (UoW) gère la notion de transaction atomique.
Il coordonne l'écriture en base de données et la collecte
des événements émis par les agrégats au cours de la transaction.

Le UoW agit comme un context manager :
    with uow:
        # ... opérations sur le repository ...
        uow.commit()
"""

from __future__ import annotations

import abc

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from allocation.adapters import repository

DEFAULT_SESSION_FACTORY = sessionmaker(
    bind=create_engine(
        "sqlite:///allocation.db",
        isolation_level="SERIALIZABLE",
    )
)


class AbstractUnitOfWork(abc.ABC):
    """
    Interface abstraite du Unit of Work.

    Fournit un repository `produits` et gère commit/rollback.
    Le rollback est automatique si commit() n'est pas appelé
    (grâce au __exit__ du context manager).
    """

    produits: repository.AbstractRepository

    def __enter__(self) -> AbstractUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        self.rollback()

    def commit(self) -> None:
        self._commit()

    def collect_new_events(self):
        """
        Collecte tous les événements émis par les agrégats vus
        pendant cette transaction.

        Parcourt les agrégats trackés par le repository (via `seen`)
        et vide leur liste d'événements pour les passer au message bus.
        """
        for produit in self.produits.seen:
            while produit.événements:
                yield produit.événements.pop(0)

    @abc.abstractmethod
    def _commit(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def rollback(self) -> None:
        raise NotImplementedError


class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    """
    Implémentation concrète du UoW avec SQLAlchemy.

    Crée une session à l'entrée du context manager,
    la ferme à la sortie. Rollback automatique si pas de commit.
    """

    def __init__(self, session_factory: sessionmaker = DEFAULT_SESSION_FACTORY):
        self.session_factory = session_factory

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self.session: Session = self.session_factory()
        self.produits = repository.SqlAlchemyRepository(self.session)
        return super().__enter__()

    def __exit__(self, *args: object) -> None:
        super().__exit__(*args)
        self.session.close()

    def _commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
