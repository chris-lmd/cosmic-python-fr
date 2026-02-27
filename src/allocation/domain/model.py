"""
Modèle de domaine pour l'allocation de stock.

Ce module contient les entités et value objects du domaine métier.
Le domaine modélise un système d'allocation de commandes (OrderLine)
à des lots de stock (Batch), regroupés au sein d'un agrégat Product.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from allocation.domain import events, commands


class OutOfStock(Exception):
    """Levée quand il n'y a plus de stock disponible pour un SKU donné."""
    pass


@dataclass(frozen=True)
class OrderLine:
    """
    Value Object représentant une ligne de commande.

    Un value object est immuable et défini par ses attributs,
    pas par une identité. Deux OrderLine avec les mêmes attributs
    sont considérées comme identiques.
    """

    orderid: str
    sku: str
    qty: int


class Batch:
    """
    Entité représentant un lot de stock.

    Un Batch a une identité (sa reference) et un cycle de vie.
    Il contient une quantité de stock pour un SKU donné,
    avec une date d'arrivée (ETA) optionnelle.
    """

    def __init__(self, ref: str, sku: str, qty: int, eta: Optional[date] = None):
        self.reference = ref
        self.sku = sku
        self.eta = eta
        self._purchased_quantity = qty
        self._allocations: set[OrderLine] = set()

    def __repr__(self) -> str:
        return f"<Batch {self.reference}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Batch):
            return NotImplemented
        return self.reference == other.reference

    def __hash__(self) -> int:
        return hash(self.reference)

    def __gt__(self, other: Batch) -> bool:
        if self.eta is None:
            return False
        if other.eta is None:
            return True
        return self.eta > other.eta

    @property
    def allocated_quantity(self) -> int:
        return sum(line.qty for line in self._allocations)

    @property
    def available_quantity(self) -> int:
        return self._purchased_quantity - self.allocated_quantity

    def allocate(self, line: OrderLine) -> None:
        """Alloue une ligne de commande à ce lot."""
        if self.can_allocate(line):
            self._allocations.add(line)

    def deallocate(self, line: OrderLine) -> None:
        """Désalloue une ligne de commande de ce lot."""
        if line in self._allocations:
            self._allocations.discard(line)

    def deallocate_one(self) -> OrderLine:
        """Désalloue et retourne une ligne de commande arbitraire."""
        return self._allocations.pop()

    def can_allocate(self, line: OrderLine) -> bool:
        """Vérifie si ce lot peut accueillir la ligne de commande."""
        return self.sku == line.sku and self.available_quantity >= line.qty


class Product:
    """
    Agrégat racine pour la gestion des produits.

    Un Product regroupe tous les Batch pour un SKU donné.
    C'est la frontière de cohérence : toutes les opérations
    d'allocation passent par cet agrégat.
    """

    def __init__(self, sku: str, batches: Optional[list[Batch]] = None, version_number: int = 0):
        self.sku = sku
        self.batches = batches or []
        self.version_number = version_number
        self.events: list[events.Event] = []

    def allocate(self, line: OrderLine) -> str:
        """
        Alloue une ligne de commande au lot le plus approprié.

        La stratégie d'allocation privilégie les lots en stock
        (sans ETA) puis les lots avec l'ETA la plus proche.

        Retourne la référence du lot choisi.
        Émet un événement OutOfStock s'il n'y a plus de stock.
        """
        try:
            batch = next(
                b for b in sorted(self.batches)
                if b.can_allocate(line)
            )
        except StopIteration:
            self.events.append(events.OutOfStock(sku=line.sku))
            return ""

        batch.allocate(line)
        self.version_number += 1
        return batch.reference

    def change_batch_quantity(self, ref: str, qty: int) -> None:
        """
        Modifie la quantité d'un lot et réalloue si nécessaire.

        Si la nouvelle quantité est inférieure aux allocations existantes,
        les lignes en excès sont désallouées et des événements
        AllocationRequired sont émis pour chacune.
        """
        batch = next(b for b in self.batches if b.reference == ref)
        batch._purchased_quantity = qty
        while batch.available_quantity < 0:
            line = batch.deallocate_one()
            self.events.append(
                events.Deallocated(
                    orderid=line.orderid,
                    sku=line.sku,
                    qty=line.qty,
                )
            )
