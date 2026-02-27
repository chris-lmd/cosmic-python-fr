"""
Tests unitaires des handlers.

Ces tests utilisent un FakeRepository et un FakeUnitOfWork
pour tester les handlers en isolation, sans base de données.
C'est l'illustration du pattern "ports and adapters" :
on remplace les adapters concrets par des fakes.
"""

from __future__ import annotations

import pytest

from allocation.domain import commands, events, model
from allocation.adapters.repository import AbstractRepository
from allocation.adapters.notifications import AbstractNotifications
from allocation.service_layer import handlers, messagebus, unit_of_work


# --- Fakes pour les tests ---


class FakeRepository(AbstractRepository):
    """
    Fake repository qui stocke les produits en mémoire.
    Utilisé pour les tests unitaires.
    """

    def __init__(self, products: list[model.Product] | None = None):
        super().__init__()
        self._products = set(products or [])

    def _add(self, product: model.Product) -> None:
        self._products.add(product)

    def _get(self, sku: str) -> model.Product | None:
        return next((p for p in self._products if p.sku == sku), None)

    def _get_by_batchref(self, batchref: str) -> model.Product | None:
        return next(
            (
                p
                for p in self._products
                for b in p.batches
                if b.reference == batchref
            ),
            None,
        )


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    """
    Fake Unit of Work utilisant le FakeRepository.
    Permet de tester sans base de données.
    """

    def __init__(self):
        self.products = FakeRepository([])
        self.committed = False

    def __enter__(self):
        return super().__enter__()

    def __exit__(self, *args):
        pass

    def _commit(self):
        self.committed = True

    def rollback(self):
        pass


class FakeNotifications(AbstractNotifications):
    """Fake pour capturer les notifications envoyées."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, destination: str, message: str) -> None:
        self.sent.append((destination, message))


def bootstrap_test_bus(uow: FakeUnitOfWork | None = None) -> messagebus.MessageBus:
    """Crée un message bus configuré pour les tests."""
    from allocation.service_layer.bootstrap import EVENT_HANDLERS, COMMAND_HANDLERS

    uow = uow or FakeUnitOfWork()
    notifications = FakeNotifications()
    return messagebus.MessageBus(
        uow=uow,
        event_handlers=EVENT_HANDLERS,
        command_handlers=COMMAND_HANDLERS,
        dependencies={"notifications": notifications},
    )


# --- Tests ---


class TestAddBatch:
    def test_add_batch_for_new_product(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "COUSSIN-CARRE", 100, None))

        assert bus.uow.products.get("COUSSIN-CARRE") is not None
        assert bus.uow.committed

    def test_add_batch_for_existing_product(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "LAMPE-RONDE", 100, None))
        bus.handle(commands.CreateBatch("b2", "LAMPE-RONDE", 99, None))

        product = bus.uow.products.get("LAMPE-RONDE")
        assert len(product.batches) == 2


class TestAllocate:
    def test_allocate_returns_batch_ref(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "CHAISE-COMFY", 100, None))
        results = bus.handle(commands.Allocate("o1", "CHAISE-COMFY", 10))

        assert results.pop(0) == "b1"

    def test_allocate_errors_for_invalid_sku(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "VRAI-SKU", 100, None))

        with pytest.raises(handlers.InvalidSku, match="SKU-INEXISTANT"):
            bus.handle(commands.Allocate("o1", "SKU-INEXISTANT", 10))


class TestChangeBatchQuantity:
    def test_changes_available_quantity(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "TAPIS-ADORABLE", 100, None))
        [batch] = bus.uow.products.get("TAPIS-ADORABLE").batches
        assert batch.available_quantity == 100

        bus.handle(commands.ChangeBatchQuantity("b1", 50))
        assert batch.available_quantity == 50

    def test_reallocates_if_necessary(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "TASSE-INDIGO", 50, None))
        bus.handle(commands.Allocate("o1", "TASSE-INDIGO", 20))
        bus.handle(commands.Allocate("o2", "TASSE-INDIGO", 20))
        assert bus.uow.products.get("TASSE-INDIGO").batches[0].available_quantity == 10

        bus.handle(commands.CreateBatch("b2", "TASSE-INDIGO", 100, None))
        bus.handle(commands.ChangeBatchQuantity("b1", 25))

        # L'une des lignes a été réallouée au nouveau batch
        assert bus.uow.products.get("TASSE-INDIGO").batches[0].available_quantity == 5
