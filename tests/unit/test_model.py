"""
Tests unitaires du modèle de domaine.

Ces tests vérifient le comportement du modèle de domaine
en isolation complète, sans base de données ni I/O.
C'est le "low gear" : on teste la logique métier au plus près.
"""

from datetime import date, timedelta

import pytest

from allocation.domain.model import Lot, LigneDeCommande, Produit


# --- Helpers ---


def créer_lot_et_ligne(
    sku: str, quantité_lot: int, quantité_ligne: int
) -> tuple[Lot, LigneDeCommande]:
    return (
        Lot("lot-001", sku, quantité_lot, eta=date.today()),
        LigneDeCommande("commande-ref", sku, quantité_ligne),
    )


# --- Tests du Lot ---


class TestLot:
    def test_allouer_réduit_la_quantité_disponible(self):
        lot, ligne = créer_lot_et_ligne("PETITE-TABLE", 20, 2)
        lot.allouer(ligne)
        assert lot.quantité_disponible == 18

    def test_peut_allouer_si_disponible_supérieur(self):
        lot, ligne = créer_lot_et_ligne("ÉLÉGANTE-LAMPE", 20, 2)
        assert lot.peut_allouer(ligne)

    def test_ne_peut_pas_allouer_si_disponible_insuffisant(self):
        lot, ligne = créer_lot_et_ligne("ÉLÉGANTE-LAMPE", 2, 20)
        assert not lot.peut_allouer(ligne)

    def test_peut_allouer_si_disponible_égal(self):
        lot, ligne = créer_lot_et_ligne("ÉLÉGANTE-LAMPE", 2, 2)
        assert lot.peut_allouer(ligne)

    def test_ne_peut_pas_allouer_si_sku_différent(self):
        lot = Lot("lot-001", "CHAISE-INCONFORTABLE", 100, eta=None)
        ligne = LigneDeCommande("commande-ref", "COUSSIN-MOELLEUX", 10)
        assert not lot.peut_allouer(ligne)

    def test_allocation_idempotente(self):
        """Allouer deux fois la même ligne n'a aucun effet (grâce au set)."""
        lot, ligne = créer_lot_et_ligne("BUREAU-ANGULAIRE", 20, 2)
        lot.allouer(ligne)
        lot.allouer(ligne)
        assert lot.quantité_disponible == 18

    def test_désallouer(self):
        lot, ligne = créer_lot_et_ligne("BUREAU-ANGULAIRE", 20, 2)
        lot.allouer(ligne)
        lot.désallouer(ligne)
        assert lot.quantité_disponible == 20

    def test_ne_désalloue_que_les_lignes_allouées(self):
        lot, ligne_non_allouée = créer_lot_et_ligne("BIBELOT-DÉCORATIF", 20, 2)
        lot.désallouer(ligne_non_allouée)
        assert lot.quantité_disponible == 20


# --- Tests de l'allocation via Produit ---


class TestProduit:
    def test_préfère_les_lots_en_stock_aux_livraisons(self):
        """Les lots en stock (sans ETA) sont préférés aux livraisons."""
        lot_en_stock = Lot("lot-stock", "HORLOGE-RÉTRO", 100, eta=None)
        lot_en_transit = Lot(
            "lot-transit", "HORLOGE-RÉTRO", 100, eta=date.today() + timedelta(days=1)
        )
        produit = Produit(sku="HORLOGE-RÉTRO", lots=[lot_en_stock, lot_en_transit])
        ligne = LigneDeCommande("cmd1", "HORLOGE-RÉTRO", 10)

        produit.allouer(ligne)

        assert lot_en_stock.quantité_disponible == 90
        assert lot_en_transit.quantité_disponible == 100

    def test_préfère_les_lots_avec_eta_la_plus_proche(self):
        """Parmi les livraisons, on préfère la plus proche."""
        plus_tôt = Lot("lot-rapide", "LAMPE-MINIMALE", 100, eta=date.today())
        moyen = Lot(
            "lot-normal", "LAMPE-MINIMALE", 100, eta=date.today() + timedelta(days=5)
        )
        plus_tard = Lot(
            "lot-lent", "LAMPE-MINIMALE", 100, eta=date.today() + timedelta(days=10)
        )
        produit = Produit(sku="LAMPE-MINIMALE", lots=[moyen, plus_tôt, plus_tard])
        ligne = LigneDeCommande("cmd1", "LAMPE-MINIMALE", 10)

        produit.allouer(ligne)

        assert plus_tôt.quantité_disponible == 90
        assert moyen.quantité_disponible == 100
        assert plus_tard.quantité_disponible == 100

    def test_retourne_la_référence_du_lot_alloué(self):
        lot_en_stock = Lot("réf-stock", "POSTER-VINTAGE", 100, eta=None)
        lot_en_transit = Lot(
            "réf-transit", "POSTER-VINTAGE", 100, eta=date.today() + timedelta(days=1)
        )
        produit = Produit(sku="POSTER-VINTAGE", lots=[lot_en_stock, lot_en_transit])
        ligne = LigneDeCommande("cmd1", "POSTER-VINTAGE", 10)

        allocation = produit.allouer(ligne)

        assert allocation == lot_en_stock.référence

    def test_émet_rupture_de_stock_si_allocation_impossible(self):
        """Un event RuptureDeStock est émis quand le stock est épuisé."""
        lot = Lot("lot1", "PETITE-FOURCHETTE", 10, eta=date.today())
        produit = Produit(sku="PETITE-FOURCHETTE", lots=[lot])

        produit.allouer(LigneDeCommande("cmd1", "PETITE-FOURCHETTE", 10))
        allocation = produit.allouer(LigneDeCommande("cmd2", "PETITE-FOURCHETTE", 1))

        from allocation.domain import events

        assert allocation == ""
        assert produit.événements[-1] == events.RuptureDeStock(sku="PETITE-FOURCHETTE")

    def test_émet_alloué_en_cas_de_succès(self):
        """Un event Alloué est émis après chaque allocation réussie."""
        lot = Lot("lot1", "TABOURET", 100, eta=None)
        produit = Produit(sku="TABOURET", lots=[lot])
        ligne = LigneDeCommande("cmd1", "TABOURET", 10)

        produit.allouer(ligne)

        from allocation.domain import events

        assert produit.événements[-1] == events.Alloué(
            id_commande="cmd1", sku="TABOURET", quantité=10, réf_lot="lot1"
        )

    def test_incrémente_le_numéro_de_version(self):
        produit = Produit(sku="TABOURET", lots=[Lot("l1", "TABOURET", 100)])
        ligne = LigneDeCommande("cmd1", "TABOURET", 10)

        produit.allouer(ligne)

        assert produit.numéro_version == 1


# --- Tests des Value Objects ---


class TestÉgalitéLigneDeCommande:
    def test_égalité(self):
        """Deux LigneDeCommande avec les mêmes attributs sont égales (value object)."""
        ligne1 = LigneDeCommande("cmd1", "SKU-001", 10)
        ligne2 = LigneDeCommande("cmd1", "SKU-001", 10)
        assert ligne1 == ligne2

    def test_inégalité(self):
        ligne1 = LigneDeCommande("cmd1", "SKU-001", 10)
        ligne2 = LigneDeCommande("cmd2", "SKU-001", 10)
        assert ligne1 != ligne2
