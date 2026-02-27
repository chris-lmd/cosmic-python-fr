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
class Alloué(Event):
    """Une LigneDeCommande a été allouée à un Lot."""

    id_commande: str
    sku: str
    quantité: int
    réf_lot: str


@dataclass(frozen=True)
class Désalloué(Event):
    """Une LigneDeCommande a été désallouée d'un Lot."""

    id_commande: str
    sku: str
    quantité: int


@dataclass(frozen=True)
class RuptureDeStock(Event):
    """Le stock est épuisé pour un SKU donné."""

    sku: str
