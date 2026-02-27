"""
Pattern Repository.

Le repository fournit une abstraction sur la couche de persistance.
Il expose une interface de type collection (add, get) qui masque
les détails de l'accès aux données.

Les noms de méthodes du pattern (add, get) restent en anglais
car ce sont des conventions reconnues. Les méthodes spécifiques
au domaine (get_par_réf_lot) sont en français.
"""

from __future__ import annotations

import abc

from sqlalchemy.orm import Session

from allocation.domain import model


class AbstractRepository(abc.ABC):
    """
    Interface abstraite du repository.

    Le pattern Template Method est utilisé : les méthodes publiques
    (add, get) gèrent le tracking via `seen`, puis délèguent
    aux méthodes abstraites préfixées _ que les sous-classes implémentent.
    """

    seen: set[model.Produit]

    def __init__(self) -> None:
        # `seen` trace tous les agrégats consultés pendant la transaction,
        # ce qui permet au Unit of Work de collecter leurs événements.
        self.seen: set[model.Produit] = set()

    def add(self, produit: model.Produit) -> None:
        """Ajoute un produit au repository et le marque comme vu."""
        self._add(produit)
        self.seen.add(produit)

    def get(self, sku: str) -> model.Produit | None:
        """Récupère un produit par son SKU et le marque comme vu."""
        produit = self._get(sku)
        if produit:
            self.seen.add(produit)
        return produit

    def get_par_réf_lot(self, réf_lot: str) -> model.Produit | None:
        """Récupère le produit contenant le lot de référence donnée."""
        produit = self._get_par_réf_lot(réf_lot)
        if produit:
            self.seen.add(produit)
        return produit

    @abc.abstractmethod
    def _add(self, produit: model.Produit) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def _get(self, sku: str) -> model.Produit | None:
        raise NotImplementedError

    @abc.abstractmethod
    def _get_par_réf_lot(self, réf_lot: str) -> model.Produit | None:
        raise NotImplementedError


class SqlAlchemyRepository(AbstractRepository):
    """Implémentation concrète du repository avec SQLAlchemy."""

    def __init__(self, session: Session):
        super().__init__()
        self.session = session

    def _add(self, produit: model.Produit) -> None:
        self.session.add(produit)

    def _get(self, sku: str) -> model.Produit | None:
        return (
            self.session.query(model.Produit)
            .filter_by(sku=sku)
            .first()
        )

    def _get_par_réf_lot(self, réf_lot: str) -> model.Produit | None:
        return (
            self.session.query(model.Produit)
            .join(model.Lot)
            .filter(model.Lot.référence == réf_lot)
            .first()
        )
