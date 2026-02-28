"""
Handlers pour les commands et events.

Les handlers sont les fonctions qui traitent les commands et events
transitant par le message bus.

- Command handlers : exécutent une action (peuvent échouer)
- Event handlers : réagissent à un fait passé (ne doivent pas échouer)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from allocation.domain import commands, events, model

if TYPE_CHECKING:
    from allocation.adapters.notifications import AbstractNotifications
    from allocation.service_layer.unit_of_work import AbstractUnitOfWork

logger = logging.getLogger(__name__)


# --- Exceptions ---


class SkuInconnu(Exception):
    """Levée quand un SKU référencé n'existe pas dans le système."""
    pass


# --- Command Handlers ---


def ajouter_lot(
    cmd: commands.CréerLot,
    uow: AbstractUnitOfWork,
) -> None:
    """
    Crée un nouveau lot de stock.

    Si le produit n'existe pas encore, il est créé automatiquement.
    """
    with uow:
        produit = uow.produits.get(sku=cmd.sku)
        if produit is None:
            produit = model.Produit(sku=cmd.sku, lots=[])
            uow.produits.add(produit)
        produit.lots.append(
            model.Lot(réf=cmd.réf, sku=cmd.sku, quantité=cmd.quantité, eta=cmd.eta)
        )
        uow.commit()


def allouer(
    cmd: commands.Allouer,
    uow: AbstractUnitOfWork,
) -> str:
    """
    Alloue une ligne de commande au lot le plus approprié.

    Retourne la référence du lot choisi.
    Lève SkuInconnu si le SKU n'existe pas.
    """
    ligne = model.LigneDeCommande(
        id_commande=cmd.id_commande, sku=cmd.sku, quantité=cmd.quantité
    )
    with uow:
        produit = uow.produits.get(sku=cmd.sku)
        if produit is None:
            raise SkuInconnu(f"SKU inconnu : {cmd.sku}")
        réf_lot = produit.allouer(ligne)
        uow.commit()
    return réf_lot


def modifier_quantité_lot(
    cmd: commands.ModifierQuantitéLot,
    uow: AbstractUnitOfWork,
) -> None:
    """
    Modifie la quantité d'un lot existant.

    Peut déclencher des réallocations si la nouvelle quantité
    est inférieure aux allocations existantes.
    """
    with uow:
        produit = uow.produits.get_par_réf_lot(réf_lot=cmd.réf)
        if produit is None:
            raise SkuInconnu(f"Lot inconnu : {cmd.réf}")
        produit.modifier_quantité_lot(réf=cmd.réf, quantité=cmd.quantité)
        uow.commit()


# --- Event Handlers ---


def publier_événement_allocation(
    event: events.Alloué,
    uow: AbstractUnitOfWork,
) -> None:
    """
    Publie un événement d'allocation vers l'extérieur.

    Dans un système complet, cela publierait vers Redis, Kafka, etc.
    Ici c'est un placeholder — voir le chapitre 11 pour l'implémentation.
    """
    logger.info(
        "Allocation publiée : %s -> %s (quantité: %d)",
        event.id_commande, event.réf_lot, event.quantité,
    )


def ajouter_allocation_vue(
    event: events.Alloué,
    uow: AbstractUnitOfWork,
) -> None:
    """
    Met à jour le read model (vue dénormalisée) après une allocation.

    C'est le côté "écriture dans la vue" du pattern CQRS :
    un event handler écoute Alloué et insère dans allocations_view.
    """
    with uow:
        uow.session.execute(
            text(
                "INSERT INTO allocations_view (id_commande, sku, réf_lot)"
                " VALUES (:id_commande, :sku, :réf_lot)"
            ),
            dict(
                id_commande=event.id_commande,
                sku=event.sku,
                réf_lot=event.réf_lot,
            ),
        )
        uow.commit()


def supprimer_allocation_vue(
    event: events.Désalloué,
    uow: AbstractUnitOfWork,
) -> None:
    """Supprime une entrée du read model après une désallocation."""
    with uow:
        uow.session.execute(
            text(
                "DELETE FROM allocations_view"
                " WHERE id_commande = :id_commande AND sku = :sku"
            ),
            dict(id_commande=event.id_commande, sku=event.sku),
        )
        uow.commit()


def réallouer(
    event: events.Désalloué,
    uow: AbstractUnitOfWork,
) -> None:
    """
    Réalloue automatiquement une ligne désallouée.

    Appelé quand un événement Désalloué est émis suite à un
    changement de quantité de lot. Crée une nouvelle command
    Allouer qui repassera dans le message bus.
    """
    allouer(
        commands.Allouer(
            id_commande=event.id_commande,
            sku=event.sku,
            quantité=event.quantité,
        ),
        uow=uow,
    )


def envoyer_notification_rupture_stock(
    event: events.RuptureDeStock,
    notifications: AbstractNotifications,
) -> None:
    """Envoie une notification quand le stock est épuisé."""
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )
