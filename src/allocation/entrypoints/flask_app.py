"""
Point d'entrée Flask.

L'API Flask est un thin adapter : elle se contente de
convertir les requêtes HTTP en commands, les envoie au
message bus, et convertit les résultats en réponses HTTP.

L'API ne contient aucune logique métier.
"""

from __future__ import annotations

from datetime import datetime

from flask import Flask, jsonify, request

from allocation.domain import commands
from allocation.service_layer import bootstrap, handlers


app = Flask(__name__)
bus = bootstrap.bootstrap()


@app.route("/add_batch", methods=["POST"])
def add_batch_endpoint():
    """
    POST /add_batch
    Body JSON : { ref, sku, qty, eta? }

    Crée un nouveau lot de stock.
    """
    data = request.json
    eta = data.get("eta")
    if eta is not None:
        eta = datetime.fromisoformat(eta).date()

    cmd = commands.CreateBatch(
        ref=data["ref"],
        sku=data["sku"],
        qty=data["qty"],
        eta=eta,
    )
    bus.handle(cmd)
    return "OK", 201


@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    """
    POST /allocate
    Body JSON : { orderid, sku, qty }

    Alloue une ligne de commande. Retourne la référence du lot.
    """
    data = request.json
    try:
        cmd = commands.Allocate(
            orderid=data["orderid"],
            sku=data["sku"],
            qty=data["qty"],
        )
        results = bus.handle(cmd)
        batchref = results.pop(0)
    except handlers.InvalidSku as e:
        return jsonify({"message": str(e)}), 400

    return jsonify({"batchref": batchref}), 201


@app.route("/allocations/<orderid>", methods=["GET"])
def allocations_view_endpoint(orderid: str):
    """
    GET /allocations/<orderid>

    Retourne les allocations pour une commande donnée (lecture CQRS).
    """
    from allocation.views import views

    result = views.allocations(orderid, bus.uow)
    if not result:
        return "not found", 404
    return jsonify(result), 200
