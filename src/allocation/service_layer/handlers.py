"""
Handlers pour les commands et events.

Les handlers sont les fonctions qui traitent les commands et events
transitant par le message bus.

- Command handlers : exécutent une action (peuvent échouer)
- Event handlers : réagissent à un fait passé (ne doivent pas échouer)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from allocation.domain import commands, events, model

if TYPE_CHECKING:
    from allocation.adapters.notifications import AbstractNotifications
    from allocation.service_layer.unit_of_work import AbstractUnitOfWork


# --- Command Handlers ---


def add_batch(
    cmd: commands.CreateBatch,
    uow: AbstractUnitOfWork,
) -> None:
    """
    Crée un nouveau lot de stock.

    Si le produit n'existe pas encore, il est créé automatiquement.
    """
    with uow:
        product = uow.products.get(sku=cmd.sku)
        if product is None:
            product = model.Product(sku=cmd.sku, batches=[])
            uow.products.add(product)
        product.batches.append(
            model.Batch(ref=cmd.ref, sku=cmd.sku, qty=cmd.qty, eta=cmd.eta)
        )
        uow.commit()


def allocate(
    cmd: commands.Allocate,
    uow: AbstractUnitOfWork,
) -> str:
    """
    Alloue une ligne de commande au lot le plus approprié.

    Retourne la référence du lot choisi.
    Lève InvalidSku si le SKU n'existe pas.
    """
    line = model.OrderLine(orderid=cmd.orderid, sku=cmd.sku, qty=cmd.qty)
    with uow:
        product = uow.products.get(sku=cmd.sku)
        if product is None:
            raise InvalidSku(f"SKU inconnu : {cmd.sku}")
        batchref = product.allocate(line)
        uow.commit()
    return batchref


def change_batch_quantity(
    cmd: commands.ChangeBatchQuantity,
    uow: AbstractUnitOfWork,
) -> None:
    """
    Modifie la quantité d'un lot existant.

    Peut déclencher des réallocations si la nouvelle quantité
    est inférieure aux allocations existantes.
    """
    with uow:
        product = uow.products.get_by_batchref(batchref=cmd.ref)
        if product is None:
            raise InvalidSku(f"Batch inconnu : {cmd.ref}")
        product.change_batch_quantity(ref=cmd.ref, qty=cmd.qty)
        uow.commit()


# --- Event Handlers ---


def publish_allocated_event(
    event: events.Allocated,
    uow: AbstractUnitOfWork,
) -> None:
    """Publie un événement d'allocation (par ex. vers Redis)."""
    # Ici on pourrait publier vers Redis, Kafka, etc.
    pass


def reallocate(
    event: events.Deallocated,
    uow: AbstractUnitOfWork,
) -> None:
    """
    Réalloue automatiquement une ligne désallouée.

    Appelé quand un Deallocated event est émis suite à un
    changement de quantité de lot.
    """
    allocate(
        commands.Allocate(
            orderid=event.orderid,
            sku=event.sku,
            qty=event.qty,
        ),
        uow=uow,
    )


def send_out_of_stock_notification(
    event: events.OutOfStock,
    notifications: AbstractNotifications,
) -> None:
    """Envoie une notification quand le stock est épuisé."""
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )


# --- Exceptions ---


class InvalidSku(Exception):
    """Levée quand un SKU référencé n'existe pas dans le système."""
    pass
