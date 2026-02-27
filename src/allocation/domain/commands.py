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
class CreateBatch(Command):
    """Demande de création d'un nouveau lot de stock."""

    ref: str
    sku: str
    qty: int
    eta: Optional[date] = None


@dataclass(frozen=True)
class Allocate(Command):
    """Demande d'allocation d'une ligne de commande."""

    orderid: str
    sku: str
    qty: int


@dataclass(frozen=True)
class ChangeBatchQuantity(Command):
    """Demande de modification de la quantité d'un lot."""

    ref: str
    qty: int
