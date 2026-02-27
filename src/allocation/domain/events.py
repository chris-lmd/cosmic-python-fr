"""
Events du domaine.

Les events représentent des faits qui se sont produits dans le système.
Ils sont immuables et nommés au passé (quelque chose s'est passé).
"""

from dataclasses import dataclass


class Event:
    """Classe de base pour tous les events du domaine."""
    pass


@dataclass(frozen=True)
class Allocated(Event):
    """Un OrderLine a été alloué à un Batch."""

    orderid: str
    sku: str
    qty: int
    batchref: str


@dataclass(frozen=True)
class Deallocated(Event):
    """Un OrderLine a été désalloué d'un Batch."""

    orderid: str
    sku: str
    qty: int


@dataclass(frozen=True)
class OutOfStock(Event):
    """Le stock est épuisé pour un SKU donné."""

    sku: str
