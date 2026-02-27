"""
Pattern Unit of Work.

Le Unit of Work (UoW) gère la notion de transaction atomique.
Il coordonne l'écriture en base de données et la collecte
des events émis par les agrégats au cours de la transaction.

Le UoW agit comme un context manager :
    with uow:
        # ... opérations sur le repository ...
        uow.commit()

Avantages :
- Garantit l'atomicité des opérations
- Centralise la gestion des sessions/transactions
- Collecte les events pour le message bus
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

    Définit le contrat : un repository products,
    et les méthodes commit/rollback.
    """

    products: repository.AbstractRepository

    def __enter__(self) -> AbstractUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        self.rollback()

    def commit(self) -> None:
        self._commit()

    def collect_new_events(self):
        """
        Collecte tous les events émis par les agrégats vus
        au cours de cette transaction.
        """
        for product in self.products.seen:
            while product.events:
                yield product.events.pop(0)

    @abc.abstractmethod
    def _commit(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def rollback(self) -> None:
        raise NotImplementedError


class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    """
    Implémentation concrète du UoW avec SQLAlchemy.

    Gère la session SQLAlchemy et le repository associé.
    """

    def __init__(self, session_factory: sessionmaker = DEFAULT_SESSION_FACTORY):
        self.session_factory = session_factory

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self.session: Session = self.session_factory()
        self.products = repository.SqlAlchemyRepository(self.session)
        return super().__enter__()

    def __exit__(self, *args: object) -> None:
        super().__exit__(*args)
        self.session.close()

    def _commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
