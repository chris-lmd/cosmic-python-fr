"""
Bootstrap : assemblage de l'application (Composition Root).

Ce module construit le message bus avec toutes ses dépendances.
C'est ici que l'injection de dépendances est réalisée :
on assemble les composants concrets (ou les fakes pour les tests).

C'est le seul endroit de l'application qui connaît les
implémentations concrètes de chaque abstraction.
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

    En production, utilise les implémentations concrètes.
    En test, on injecte des fakes via les paramètres.
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


# --- Routage des messages vers les handlers ---

EVENT_HANDLERS: dict[type[events.Event], list] = {
    events.Alloué: [
        handlers.publier_événement_allocation,
        handlers.ajouter_allocation_vue,
    ],
    events.Désalloué: [
        handlers.réallouer,
        handlers.supprimer_allocation_vue,
    ],
    events.RuptureDeStock: [handlers.envoyer_notification_rupture_stock],
}

COMMAND_HANDLERS: dict[type[commands.Command], Any] = {
    commands.CréerLot: handlers.ajouter_lot,
    commands.Allouer: handlers.allouer,
    commands.ModifierQuantitéLot: handlers.modifier_quantité_lot,
}
