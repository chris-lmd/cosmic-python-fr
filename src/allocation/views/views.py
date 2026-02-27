"""
Views (lecture) pour le pattern CQRS.

Les views sont des fonctions de lecture pure qui interrogent
directement la base de données, sans passer par le modèle de domaine.

C'est le côté Query de CQRS : on sépare les chemins d'écriture
(qui passent par le domaine et le message bus) des chemins de
lecture (qui interrogent directement la BDD pour la performance).
"""

from __future__ import annotations

from sqlalchemy import text

from allocation.service_layer import unit_of_work


def allocations(id_commande: str, uow: unit_of_work.AbstractUnitOfWork) -> list[dict]:
    """
    Retourne les allocations pour un id_commande donné.

    Requête SQL directe sur la table de lecture (read model),
    sans charger d'agrégat — c'est tout l'intérêt de CQRS.
    """
    with uow:
        results = uow.session.execute(
            text("SELECT sku, réf_lot FROM allocations_view WHERE id_commande = :id_commande"),
            dict(id_commande=id_commande),
        )
        return [dict(r._mapping) for r in results]
