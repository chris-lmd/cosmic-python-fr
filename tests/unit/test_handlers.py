"""
Tests des handlers via la service layer (high gear).

Ces tests utilisent des fakes (FakeRepository, FakeUnitOfWork)
pour tester le comportement métier sans base de données ni I/O.
C'est le "high gear" : on teste les cas d'usage complets.
"""

from __future__ import annotations

import pytest

from allocation.domain import commands, events
from allocation.domain.model import Produit, Lot, LigneDeCommande
from allocation.adapters.repository import AbstractRepository
from allocation.adapters.notifications import AbstractNotifications
from allocation.service_layer import bootstrap, handlers, messagebus, unit_of_work


# --- Fakes pour les tests ---


class FakeRepository(AbstractRepository):
    """
    Repository en mémoire pour les tests.

    Utilise un set Python au lieu d'une base de données.
    Hérite d'AbstractRepository pour bénéficier du tracking `seen`.
    """

    def __init__(self, produits: list[Produit] | None = None):
        super().__init__()
        self._produits = set(produits or [])

    def _add(self, produit: Produit) -> None:
        self._produits.add(produit)

    def _get(self, sku: str) -> Produit | None:
        return next((p for p in self._produits if p.sku == sku), None)

    def _get_par_réf_lot(self, réf_lot: str) -> Produit | None:
        return next(
            (p for p in self._produits
             for l in p.lots
             if l.référence == réf_lot),
            None,
        )


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    """
    Unit of Work en mémoire pour les tests.

    L'attribut `committed` permet de vérifier que le commit
    a bien été appelé dans les tests.
    """

    def __init__(self) -> None:
        self.produits = FakeRepository()
        self.committed = False

    def __enter__(self) -> FakeUnitOfWork:
        return super().__enter__()

    def _commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        pass


class FakeNotifications(AbstractNotifications):
    """Capture les notifications envoyées pour vérification dans les tests."""

    def __init__(self) -> None:
        self.envoyées: list[tuple[str, str]] = []

    def send(self, destination: str, message: str) -> None:
        self.envoyées.append((destination, message))


# --- Bootstrap de test ---


def bootstrap_test_bus(
    uow: FakeUnitOfWork | None = None,
    notifications: FakeNotifications | None = None,
) -> messagebus.MessageBus:
    """
    Construit un MessageBus configuré avec des fakes.

    Même wiring que la production, mais avec des implémentations
    en mémoire pour l'isolation et la rapidité.
    """
    if uow is None:
        uow = FakeUnitOfWork()
    if notifications is None:
        notifications = FakeNotifications()
    return bootstrap.bootstrap(
        start_orm=False,
        uow=uow,
        notifications_adapter=notifications,
    )


# --- Tests des Commands ---


class TestAjouterLot:
    def test_ajouter_un_lot(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("lot-001", "TABOURET-ROUGE", 100, None))

        produit = bus.uow.produits.get("TABOURET-ROUGE")
        assert produit is not None
        assert len(produit.lots) == 1
        assert produit.lots[0].référence == "lot-001"


class TestAllouer:
    def test_allouer_retourne_la_référence_du_lot(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("lot-001", "LAMPE-DESIGN", 100, None))

        results = bus.handle(commands.Allouer("cmd-001", "LAMPE-DESIGN", 10))

        assert results[0] == "lot-001"

    def test_allouer_lève_sku_inconnu(self):
        bus = bootstrap_test_bus()

        with pytest.raises(handlers.SkuInconnu, match="SKU-INEXISTANT"):
            bus.handle(commands.Allouer("cmd-001", "SKU-INEXISTANT", 10))


class TestModifierQuantitéLot:
    def test_réalloue_si_quantité_réduite(self):
        """
        Quand on réduit la quantité d'un lot en dessous des allocations,
        les lignes en excès sont désallouées puis réallouées ailleurs.
        """
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("lot-001", "CHAISE-BLEUE", 50, None))
        bus.handle(commands.CréerLot("lot-002", "CHAISE-BLEUE", 50, None))
        bus.handle(commands.Allouer("cmd-001", "CHAISE-BLEUE", 20))
        bus.handle(commands.Allouer("cmd-002", "CHAISE-BLEUE", 20))

        # On réduit lot-001 à 25 alors que 40 y sont allouées
        bus.handle(commands.ModifierQuantitéLot("lot-001", 25))

        # Les lignes en excès ont été réallouées vers lot-002
        produit = bus.uow.produits.get("CHAISE-BLEUE")
        lot_001 = next(l for l in produit.lots if l.référence == "lot-001")
        lot_002 = next(l for l in produit.lots if l.référence == "lot-002")
        # Le total alloué reste 40, réparti entre les deux lots
        assert lot_001.quantité_allouée + lot_002.quantité_allouée == 40


class TestNotificationRuptureDeStock:
    def test_envoie_notification_si_rupture(self):
        """Vérifie que la notification est envoyée quand le stock est épuisé."""
        notifications = FakeNotifications()
        bus = bootstrap_test_bus(notifications=notifications)
        bus.handle(commands.CréerLot("lot-001", "LAMPE-RARE", 10, None))
        bus.handle(commands.Allouer("cmd-001", "LAMPE-RARE", 10))

        # Cette allocation échoue car le stock est épuisé
        bus.handle(commands.Allouer("cmd-002", "LAMPE-RARE", 1))

        assert len(notifications.envoyées) == 1
        assert "LAMPE-RARE" in notifications.envoyées[0][1]
