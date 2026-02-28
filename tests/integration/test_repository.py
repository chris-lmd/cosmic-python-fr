"""
Tests d'intégration du Repository avec SQLite en mémoire.

Ces tests vérifient que le mapping ORM fonctionne correctement :
- Sauvegarder et recharger un Produit avec ses Lots
- Les allocations survivent à un aller-retour en BDD
- La recherche par référence de lot fonctionne
"""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from allocation.adapters import orm, repository
from allocation.domain.model import LigneDeCommande, Lot, Produit


def make_session():
    """Crée une session SQLite en mémoire avec les tables."""
    engine = create_engine("sqlite:///:memory:")
    orm.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestSqlAlchemyRepository:
    def test_sauvegarder_et_recharger_un_produit(self):
        session = make_session()
        repo = repository.SqlAlchemyRepository(session)
        produit = Produit(sku="TABLE-ROUGE", lots=[
            Lot("lot-001", "TABLE-ROUGE", 100, eta=None),
            Lot("lot-002", "TABLE-ROUGE", 50, eta=date(2025, 6, 15)),
        ])

        repo.add(produit)
        session.commit()

        # Recharger depuis la BDD
        rechargé = repo.get("TABLE-ROUGE")
        assert rechargé is not None
        assert rechargé.sku == "TABLE-ROUGE"
        assert len(rechargé.lots) == 2
        refs = {lot.référence for lot in rechargé.lots}
        assert refs == {"lot-001", "lot-002"}

    def test_les_allocations_survivent_au_rechargement(self):
        session = make_session()
        repo = repository.SqlAlchemyRepository(session)
        lot = Lot("lot-001", "LAMPE-BLEUE", 100, eta=None)
        produit = Produit(sku="LAMPE-BLEUE", lots=[lot])
        ligne = LigneDeCommande("commande-1", "LAMPE-BLEUE", 10)
        lot.allouer(ligne)

        repo.add(produit)
        session.commit()

        rechargé = repo.get("LAMPE-BLEUE")
        lot_rechargé = rechargé.lots[0]
        assert lot_rechargé.quantité_disponible == 90

    def test_get_par_réf_lot(self):
        session = make_session()
        repo = repository.SqlAlchemyRepository(session)
        produit = Produit(sku="CHAISE-VERTE", lots=[
            Lot("lot-abc", "CHAISE-VERTE", 50, eta=None),
        ])

        repo.add(produit)
        session.commit()

        trouvé = repo.get_par_réf_lot("lot-abc")
        assert trouvé is not None
        assert trouvé.sku == "CHAISE-VERTE"

    def test_get_retourne_none_si_sku_inexistant(self):
        session = make_session()
        repo = repository.SqlAlchemyRepository(session)

        assert repo.get("INEXISTANT") is None

    def test_seen_trace_les_agrégats(self):
        session = make_session()
        repo = repository.SqlAlchemyRepository(session)
        produit = Produit(sku="MIROIR-ROND", lots=[])

        repo.add(produit)
        session.commit()

        assert produit in repo.seen
        repo2 = repository.SqlAlchemyRepository(session)
        repo2.get("MIROIR-ROND")
        assert len(repo2.seen) == 1
