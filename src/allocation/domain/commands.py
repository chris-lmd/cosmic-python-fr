"""
Commands du domaine.

Les commands représentent des intentions : quelque chose que
le système doit faire. Contrairement aux events (faits passés),
les commands sont des demandes qui peuvent échouer.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional


class Command:
    """Classe de base pour toutes les commands."""
    pass


@dataclass(frozen=True)
class CréerLot(Command):
    """Demande de création d'un nouveau lot de stock."""

    réf: str
    sku: str
    quantité: int
    eta: Optional[date] = None


@dataclass(frozen=True)
class Allouer(Command):
    """Demande d'allocation d'une ligne de commande."""

    id_commande: str
    sku: str
    quantité: int


@dataclass(frozen=True)
class ModifierQuantitéLot(Command):
    """Demande de modification de la quantité d'un lot."""

    réf: str
    quantité: int
