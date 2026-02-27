"""
Tests unitaires du modèle de domaine.

Ces tests vérifient le comportement du modèle de domaine
en isolation complète, sans base de données ni I/O.
"""

from datetime import date, timedelta

import pytest

from allocation.domain.model import Batch, OrderLine, Product


# --- Helpers ---


def make_batch_and_line(
    sku: str, batch_qty: int, line_qty: int
) -> tuple[Batch, OrderLine]:
    return (
        Batch("batch-001", sku, batch_qty, eta=date.today()),
        OrderLine("order-ref", sku, line_qty),
    )


# --- Tests du Batch ---


class TestBatch:
    def test_allocating_reduces_available_quantity(self):
        batch, line = make_batch_and_line("PETITE-TABLE", 20, 2)
        batch.allocate(line)
        assert batch.available_quantity == 18

    def test_can_allocate_if_available_greater_than_required(self):
        batch, line = make_batch_and_line("ELEGANTE-LAMPE", 20, 2)
        assert batch.can_allocate(line)

    def test_cannot_allocate_if_available_smaller_than_required(self):
        batch, line = make_batch_and_line("ELEGANTE-LAMPE", 2, 20)
        assert not batch.can_allocate(line)

    def test_can_allocate_if_available_equal_to_required(self):
        batch, line = make_batch_and_line("ELEGANTE-LAMPE", 2, 2)
        assert batch.can_allocate(line)

    def test_cannot_allocate_if_skus_do_not_match(self):
        batch = Batch("batch-001", "CHAISE-INCOMFORTABLE", 100, eta=None)
        line = OrderLine("order-ref", "COUSSIN-MOELLEUX", 10)
        assert not batch.can_allocate(line)

    def test_allocation_is_idempotent(self):
        batch, line = make_batch_and_line("ANGULAR-DESK", 20, 2)
        batch.allocate(line)
        batch.allocate(line)
        assert batch.available_quantity == 18

    def test_deallocate(self):
        batch, line = make_batch_and_line("ANGULAR-DESK", 20, 2)
        batch.allocate(line)
        batch.deallocate(line)
        assert batch.available_quantity == 20

    def test_can_only_deallocate_allocated_lines(self):
        batch, unallocated_line = make_batch_and_line("DECORATIVE-TRINKET", 20, 2)
        batch.deallocate(unallocated_line)
        assert batch.available_quantity == 20


# --- Tests de l'allocation via Product ---


class TestProduct:
    def test_prefers_warehouse_batches_to_shipments(self):
        """Les lots en stock (sans ETA) sont préférés aux livraisons."""
        in_stock_batch = Batch("in-stock-batch", "HORLOGE-RETRO", 100, eta=None)
        shipment_batch = Batch(
            "shipment-batch", "HORLOGE-RETRO", 100, eta=date.today() + timedelta(days=1)
        )
        product = Product(sku="HORLOGE-RETRO", batches=[in_stock_batch, shipment_batch])
        line = OrderLine("oref", "HORLOGE-RETRO", 10)

        product.allocate(line)

        assert in_stock_batch.available_quantity == 90
        assert shipment_batch.available_quantity == 100

    def test_prefers_earlier_batches(self):
        """Parmi les livraisons, on préfère la plus proche."""
        earliest = Batch("speedy-batch", "LAMPE-MINIMALE", 100, eta=date.today())
        medium = Batch(
            "normal-batch", "LAMPE-MINIMALE", 100, eta=date.today() + timedelta(days=5)
        )
        latest = Batch(
            "slow-batch", "LAMPE-MINIMALE", 100, eta=date.today() + timedelta(days=10)
        )
        product = Product(sku="LAMPE-MINIMALE", batches=[medium, earliest, latest])
        line = OrderLine("order1", "LAMPE-MINIMALE", 10)

        product.allocate(line)

        assert earliest.available_quantity == 90
        assert medium.available_quantity == 100
        assert latest.available_quantity == 100

    def test_returns_allocated_batch_ref(self):
        in_stock_batch = Batch("in-stock-batch-ref", "POSTER-VINTAGE", 100, eta=None)
        shipment_batch = Batch(
            "shipment-batch-ref",
            "POSTER-VINTAGE",
            100,
            eta=date.today() + timedelta(days=1),
        )
        product = Product(
            sku="POSTER-VINTAGE", batches=[in_stock_batch, shipment_batch]
        )
        line = OrderLine("oref", "POSTER-VINTAGE", 10)

        allocation = product.allocate(line)

        assert allocation == in_stock_batch.reference

    def test_outputs_out_of_stock_event_if_cannot_allocate(self):
        """Un event OutOfStock est émis quand le stock est épuisé."""
        batch = Batch("batch1", "FOURCHETTE-PETITE", 10, eta=date.today())
        product = Product(sku="FOURCHETTE-PETITE", batches=[batch])

        product.allocate(OrderLine("order1", "FOURCHETTE-PETITE", 10))
        allocation = product.allocate(OrderLine("order2", "FOURCHETTE-PETITE", 1))

        from allocation.domain import events

        assert allocation == ""
        assert product.events[-1] == events.OutOfStock(sku="FOURCHETTE-PETITE")

    def test_increments_version_number(self):
        product = Product(sku="TABOURET", batches=[Batch("b1", "TABOURET", 100)])
        line = OrderLine("oref", "TABOURET", 10)

        product.allocate(line)

        assert product.version_number == 1


# --- Tests des Value Objects ---


class TestOrderLineEquality:
    def test_equality(self):
        """Deux OrderLine avec les mêmes attributs sont égales (value object)."""
        line1 = OrderLine("order1", "SKU-001", 10)
        line2 = OrderLine("order1", "SKU-001", 10)
        assert line1 == line2

    def test_inequality(self):
        line1 = OrderLine("order1", "SKU-001", 10)
        line2 = OrderLine("order2", "SKU-001", 10)
        assert line1 != line2
