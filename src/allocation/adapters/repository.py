"""
Pattern Repository.

Le repository fournit une abstraction sur la couche de persistance.
Il expose une interface de type collection (add, get) qui masque
les détails de l'accès aux données.

Cela permet :
- Au modèle de domaine de rester indépendant de la BDD
- De substituer facilement l'implémentation (tests, migration)
- De découpler le code métier de l'infrastructure
"""

from __future__ import annotations

import abc

from sqlalchemy.orm import Session

from allocation.domain import model


class AbstractRepository(abc.ABC):
    """
    Interface abstraite du repository.

    Définit le contrat que tout repository doit respecter.
    Le pattern repose sur deux opérations fondamentales :
    - add : ajouter un nouvel agrégat
    - get : récupérer un agrégat existant
    """

    seen: set[model.Product]

    def __init__(self) -> None:
        self.seen: set[model.Product] = set()

    def add(self, product: model.Product) -> None:
        """Ajoute un produit au repository et le marque comme vu."""
        self._add(product)
        self.seen.add(product)

    def get(self, sku: str) -> model.Product | None:
        """Récupère un produit par son SKU et le marque comme vu."""
        product = self._get(sku)
        if product:
            self.seen.add(product)
        return product

    def get_by_batchref(self, batchref: str) -> model.Product | None:
        """Récupère un produit contenant le batch de référence donnée."""
        product = self._get_by_batchref(batchref)
        if product:
            self.seen.add(product)
        return product

    @abc.abstractmethod
    def _add(self, product: model.Product) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def _get(self, sku: str) -> model.Product | None:
        raise NotImplementedError

    @abc.abstractmethod
    def _get_by_batchref(self, batchref: str) -> model.Product | None:
        raise NotImplementedError


class SqlAlchemyRepository(AbstractRepository):
    """
    Implémentation concrète du repository avec SQLAlchemy.

    Utilise une session SQLAlchemy pour persister et récupérer
    les agrégats Product.
    """

    def __init__(self, session: Session):
        super().__init__()
        self.session = session

    def _add(self, product: model.Product) -> None:
        self.session.add(product)

    def _get(self, sku: str) -> model.Product | None:
        return (
            self.session.query(model.Product)
            .filter_by(sku=sku)
            .first()
        )

    def _get_by_batchref(self, batchref: str) -> model.Product | None:
        return (
            self.session.query(model.Product)
            .join(model.Batch)
            .filter(model.Batch.reference == batchref)
            .first()
        )
