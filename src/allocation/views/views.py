"""
Views (lecture) pour le pattern CQRS.

Les views sont des fonctions de lecture pure qui interrogent
directement la base de données, sans passer par le modèle de domaine.

C'est le côté Query de CQRS : on sépare les chemins d'écriture
(qui passent par le domaine et le message bus) des chemins de
lecture (qui interrogent directement la BDD pour la performance).
"""

from __future__ import annotations

from allocation.service_layer import unit_of_work


def allocations(orderid: str, uow: unit_of_work.SqlAlchemyUnitOfWork) -> list[dict]:
    """
    Retourne les allocations pour un orderid donné.

    Requête SQL directe sur la table de lecture (read model).
    """
    with uow:
        results = uow.session.execute(
            "SELECT sku, batchref FROM allocations_view WHERE orderid = :orderid",
            dict(orderid=orderid),
        )
        return [dict(r._mapping) for r in results]
