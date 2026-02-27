"""
Mapping ORM avec SQLAlchemy.

Ce module utilise le classical mapping de SQLAlchemy :
on définit les tables séparément, puis on mappe les classes
du domaine sur ces tables. Cela permet au modèle de domaine
de rester ignorant de la persistance (persistence ignorance).
"""

from sqlalchemy import (
    Column,
    Date,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    event,
)
from sqlalchemy.orm import registry, relationship

from allocation.domain import model

metadata = MetaData()
mapper_registry = registry(metadata=metadata)

# --- Définition des tables ---

order_lines = Table(
    "order_lines",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("orderid", String(255)),
    Column("sku", String(255)),
    Column("qty", Integer),
)

products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sku", String(255)),
    Column("version_number", Integer, nullable=False, server_default="0"),
)

batches = Table(
    "batches",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reference", String(255)),
    Column("sku", String(255)),
    Column("_purchased_quantity", Integer),
    Column("eta", Date, nullable=True),
    Column("product_sku", String(255), ForeignKey("products.sku")),
)

allocations = Table(
    "allocations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("orderline_id", Integer, ForeignKey("order_lines.id")),
    Column("batch_id", Integer, ForeignKey("batches.id")),
)

allocations_view = Table(
    "allocations_view",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("orderid", String(255)),
    Column("sku", String(255)),
    Column("batchref", String(255)),
)


def start_mappers() -> None:
    """
    Configure le mapping entre les classes du domaine et les tables SQL.

    Utilise le classical mapping de SQLAlchemy pour que
    le modèle de domaine ne dépende pas de SQLAlchemy.
    """
    lines_mapper = mapper_registry.map_imperatively(model.OrderLine, order_lines)
    batches_mapper = mapper_registry.map_imperatively(
        model.Batch,
        batches,
        properties={
            "_allocations": relationship(lines_mapper, secondary=allocations, collection_class=set),
        },
    )
    mapper_registry.map_imperatively(
        model.Product,
        products,
        properties={
            "batches": relationship(batches_mapper, primaryjoin=(products.c.sku == batches.c.product_sku)),
        },
    )


@event.listens_for(model.Product, "load")
def receive_load(product: model.Product, _: object) -> None:
    """Initialise la liste d'events quand un Product est chargé depuis la BDD."""
    product.events = []
