"""
Bootstrap : assemblage de l'application.

Ce module construit le message bus avec toutes ses dépendances.
C'est ici que l'injection de dépendances est réalisée :
on assemble les composants concrets (ou les fakes pour les tests).

C'est la composition root de l'application.
"""

from __future__ import annotations

from typing import Any

from allocation.adapters import notifications, orm
from allocation.domain import commands, events
from allocation.service_layer import handlers, messagebus, unit_of_work


def bootstrap(
    start_orm: bool = True,
    uow: unit_of_work.AbstractUnitOfWork | None = None,
    notifications_adapter: notifications.AbstractNotifications | None = None,
    **extra_dependencies: Any,
) -> messagebus.MessageBus:
    """
    Construit et retourne un MessageBus configuré.

    Paramètres :
    - start_orm : si True, initialise le mapping SQLAlchemy
    - uow : Unit of Work à utiliser (défaut : SqlAlchemyUnitOfWork)
    - notifications_adapter : adapter de notifications
    - extra_dependencies : dépendances supplémentaires à injecter
    """
    if start_orm:
        orm.start_mappers()

    if uow is None:
        uow = unit_of_work.SqlAlchemyUnitOfWork()

    if notifications_adapter is None:
        notifications_adapter = notifications.EmailNotifications()

    dependencies: dict[str, Any] = {
        "notifications": notifications_adapter,
        **extra_dependencies,
    }

    return messagebus.MessageBus(
        uow=uow,
        event_handlers=EVENT_HANDLERS,
        command_handlers=COMMAND_HANDLERS,
        dependencies=dependencies,
    )


# --- Configuration des handlers ---

EVENT_HANDLERS: dict[type[events.Event], list] = {
    events.Allocated: [handlers.publish_allocated_event],
    events.Deallocated: [handlers.reallocate],
    events.OutOfStock: [handlers.send_out_of_stock_notification],
}

COMMAND_HANDLERS: dict[type[commands.Command], Any] = {
    commands.CreateBatch: handlers.add_batch,
    commands.Allocate: handlers.allocate,
    commands.ChangeBatchQuantity: handlers.change_batch_quantity,
}
