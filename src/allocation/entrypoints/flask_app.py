"""
Point d'entrée Flask (thin adapter).

L'API Flask se contente de convertir les requêtes HTTP en commands,
les envoie au message bus, et convertit les résultats en réponses HTTP.
Aucune logique métier ici.
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
    """POST /add_batch — Crée un nouveau lot de stock."""
    data = request.json
    eta = data.get("eta")
    if eta is not None:
        eta = datetime.fromisoformat(eta).date()

    cmd = commands.CréerLot(
        réf=data["ref"],
        sku=data["sku"],
        quantité=data["qty"],
        eta=eta,
    )
    bus.handle(cmd)
    return "OK", 201


@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    """POST /allocate — Alloue une ligne de commande."""
    data = request.json
    try:
        cmd = commands.Allouer(
            id_commande=data["orderid"],
            sku=data["sku"],
            quantité=data["qty"],
        )
        results = bus.handle(cmd)
        réf_lot = results.pop(0)
    except handlers.SkuInconnu as e:
        return jsonify({"message": str(e)}), 400

    return jsonify({"batchref": réf_lot}), 201


@app.route("/allocations/<id_commande>", methods=["GET"])
def allocations_view_endpoint(id_commande: str):
    """GET /allocations/<id_commande> — Lecture CQRS des allocations."""
    from allocation.views import views

    result = views.allocations(id_commande, bus.uow)
    if not result:
        return "not found", 404
    return jsonify(result), 200
