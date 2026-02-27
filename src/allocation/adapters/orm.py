"""
Mapping ORM avec SQLAlchemy (classical mapping).

On définit les tables séparément, puis on mappe les classes
du domaine sur ces tables. Cela permet au modèle de domaine
de rester ignorant de la persistance (persistence ignorance).

Les noms de colonnes SQL restent en ASCII pour la compatibilité,
le mapping traduit vers les attributs français du domaine.
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
    Column("id_commande", String(255)),
    Column("sku", String(255)),
    Column("quantite", Integer),
)

products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sku", String(255)),
    Column("numero_version", Integer, nullable=False, server_default="0"),
)

batches = Table(
    "batches",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reference", String(255)),
    Column("sku", String(255)),
    Column("quantite_achetee", Integer),
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
    Column("id_commande", String(255)),
    Column("sku", String(255)),
    Column("réf_lot", String(255)),
)


def start_mappers() -> None:
    """
    Configure le mapping entre les classes du domaine et les tables SQL.

    Utilise le classical mapping : les classes du domaine ne connaissent
    pas SQLAlchemy. C'est ici qu'on fait le pont entre les attributs
    français du domaine et les colonnes de la base de données.
    """
    lines_mapper = mapper_registry.map_imperatively(
        model.LigneDeCommande,
        order_lines,
        properties={
            "id_commande": order_lines.c.id_commande,
            "quantité": order_lines.c.quantite,
        },
    )
    batches_mapper = mapper_registry.map_imperatively(
        model.Lot,
        batches,
        properties={
            "référence": batches.c.reference,
            "_quantité_achetée": batches.c.quantite_achetee,
            "_allocations": relationship(
                lines_mapper, secondary=allocations, collection_class=set
            ),
        },
    )
    mapper_registry.map_imperatively(
        model.Produit,
        products,
        properties={
            "numéro_version": products.c.numero_version,
            "lots": relationship(
                batches_mapper,
                primaryjoin=(products.c.sku == batches.c.product_sku),
            ),
        },
    )


@event.listens_for(model.Produit, "load")
def receive_load(produit: model.Produit, _: object) -> None:
    """Initialise la liste d'événements quand un Produit est chargé depuis la BDD."""
    produit.événements = []
