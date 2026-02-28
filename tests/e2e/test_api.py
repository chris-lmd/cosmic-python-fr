"""
Tests end-to-end de l'API Flask.

Ces tests vérifient le flux complet :
HTTP request → Flask → Message Bus → Handlers → Repository → SQLite

On utilise le test client Flask avec une base SQLite en mémoire,
ce qui donne des tests rapides tout en couvrant toute la chaîne.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from allocation.adapters import orm, notifications
from allocation.entrypoints.flask_app import app
from allocation.service_layer import bootstrap, unit_of_work


class FakeNotifications(notifications.AbstractNotifications):
    def __init__(self):
        self.envoyés = []

    def send(self, destination: str, message: str) -> None:
        self.envoyés.append({"destination": destination, "message": message})


@pytest.fixture
def sqlite_bus():
    """Crée un message bus configuré avec SQLite en mémoire."""
    engine = create_engine("sqlite:///:memory:")
    orm.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    uow = unit_of_work.SqlAlchemyUnitOfWork(session_factory=session_factory)
    bus = bootstrap.bootstrap(
        start_orm=False,
        uow=uow,
        notifications_adapter=FakeNotifications(),
    )
    return bus


@pytest.fixture
def client(sqlite_bus):
    """Client de test Flask avec le bus injecté."""
    import allocation.entrypoints.flask_app as flask_module

    original_bus = flask_module.bus
    flask_module.bus = sqlite_bus
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield client

    flask_module.bus = original_bus


class TestAddBatch:
    def test_créer_un_lot(self, client):
        response = client.post("/add_batch", json={
            "ref": "lot-001",
            "sku": "PETITE-TABLE",
            "qty": 100,
        })
        assert response.status_code == 201

    def test_créer_un_lot_avec_eta(self, client):
        response = client.post("/add_batch", json={
            "ref": "lot-002",
            "sku": "GRANDE-LAMPE",
            "qty": 50,
            "eta": "2025-06-15",
        })
        assert response.status_code == 201


class TestAllocate:
    def test_allouer_retourne_la_référence_du_lot(self, client):
        client.post("/add_batch", json={
            "ref": "lot-001",
            "sku": "HORLOGE-RETRO",
            "qty": 100,
        })

        response = client.post("/allocate", json={
            "orderid": "commande-1",
            "sku": "HORLOGE-RETRO",
            "qty": 10,
        })

        assert response.status_code == 201
        assert response.get_json()["batchref"] == "lot-001"

    def test_allouer_sku_inconnu_retourne_400(self, client):
        response = client.post("/allocate", json={
            "orderid": "commande-1",
            "sku": "INEXISTANT",
            "qty": 10,
        })

        assert response.status_code == 400
        assert "SKU inconnu" in response.get_json()["message"]

    def test_allouer_préfère_le_stock_en_entrepôt(self, client):
        client.post("/add_batch", json={
            "ref": "lot-en-stock",
            "sku": "VASE-BLEU",
            "qty": 100,
        })
        client.post("/add_batch", json={
            "ref": "lot-en-transit",
            "sku": "VASE-BLEU",
            "qty": 100,
            "eta": "2025-12-01",
        })

        response = client.post("/allocate", json={
            "orderid": "commande-1",
            "sku": "VASE-BLEU",
            "qty": 10,
        })

        assert response.get_json()["batchref"] == "lot-en-stock"


class TestAllocationsView:
    def test_lire_les_allocations(self, client):
        client.post("/add_batch", json={
            "ref": "lot-001",
            "sku": "COUSSIN-ROUGE",
            "qty": 100,
        })
        client.post("/allocate", json={
            "orderid": "commande-42",
            "sku": "COUSSIN-ROUGE",
            "qty": 5,
        })

        response = client.get("/allocations/commande-42")

        assert response.status_code == 200
        data = response.get_json()
        assert len(data) == 1
        assert data[0]["sku"] == "COUSSIN-ROUGE"
        assert data[0]["réf_lot"] == "lot-001"

    def test_allocations_inexistantes_retourne_404(self, client):
        response = client.get("/allocations/commande-inexistante")
        assert response.status_code == 404
