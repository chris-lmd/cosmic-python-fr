"""
Modèle de domaine pour l'allocation de stock.

Ce module contient les entités et value objects du domaine métier.
Le domaine modélise un système d'allocation de commandes (LigneDeCommande)
à des lots de stock (Lot), regroupés au sein d'un agrégat Produit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from allocation.domain import events


class RuptureDeStock(Exception):
    """Levée quand il n'y a plus de stock disponible pour un SKU donné."""
    pass


@dataclass(frozen=True)
class LigneDeCommande:
    """
    Value Object représentant une ligne de commande.

    Un value object est immuable et défini par ses attributs,
    pas par une identité. Deux LigneDeCommande avec les mêmes
    attributs sont considérées comme identiques.

    frozen=True rend la dataclass immuable et hashable,
    ce qui permet de l'utiliser dans un set (pour les allocations).
    """

    id_commande: str
    sku: str
    quantité: int


class Lot:
    """
    Entité représentant un lot de stock.

    Un Lot a une identité (sa référence) et un cycle de vie.
    Il contient une quantité de stock pour un SKU donné,
    avec une date d'arrivée (ETA) optionnelle.

    L'égalité et le hash sont basés sur la référence (identité),
    pas sur les attributs (contrairement à un Value Object).
    """

    def __init__(self, réf: str, sku: str, quantité: int, eta: Optional[date] = None):
        self.référence = réf
        self.sku = sku
        self.eta = eta
        self._quantité_achetée = quantité
        # Un set garantit l'idempotence : allouer deux fois la même ligne
        # n'a aucun effet (car LigneDeCommande est frozen/hashable).
        self._allocations: set[LigneDeCommande] = set()

    def __repr__(self) -> str:
        return f"<Lot {self.référence}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Lot):
            return NotImplemented
        return self.référence == other.référence

    def __hash__(self) -> int:
        return hash(self.référence)

    def __gt__(self, other: Lot) -> bool:
        """
        Permet de trier les lots pour la stratégie d'allocation :
        - Les lots en stock (eta=None) viennent en premier
        - Parmi les lots en transit, celui avec l'ETA la plus proche est préféré
        """
        if self.eta is None:
            return False
        if other.eta is None:
            return True
        return self.eta > other.eta

    @property
    def quantité_allouée(self) -> int:
        """Somme des quantités actuellement allouées à ce lot."""
        return sum(ligne.quantité for ligne in self._allocations)

    @property
    def quantité_disponible(self) -> int:
        """Quantité restante pouvant être allouée."""
        return self._quantité_achetée - self.quantité_allouée

    def allouer(self, ligne: LigneDeCommande) -> None:
        """Alloue une ligne de commande à ce lot (idempotent grâce au set)."""
        if self.peut_allouer(ligne):
            self._allocations.add(ligne)

    def désallouer(self, ligne: LigneDeCommande) -> None:
        """Désalloue une ligne de commande de ce lot."""
        if ligne in self._allocations:
            self._allocations.discard(ligne)

    def désallouer_une(self) -> LigneDeCommande:
        """Désalloue et retourne une ligne de commande arbitraire."""
        return self._allocations.pop()

    def peut_allouer(self, ligne: LigneDeCommande) -> bool:
        """Vérifie que le SKU correspond et que la quantité disponible suffit."""
        return self.sku == ligne.sku and self.quantité_disponible >= ligne.quantité


class Produit:
    """
    Agrégat racine pour la gestion des produits.

    Un Produit regroupe tous les Lot pour un SKU donné.
    C'est la frontière de cohérence : toutes les opérations
    d'allocation passent par cet agrégat, qui garantit les
    invariants métier et émet les événements du domaine.
    """

    def __init__(self, sku: str, lots: Optional[list[Lot]] = None, numéro_version: int = 0):
        self.sku = sku
        self.lots = lots or []
        self.numéro_version = numéro_version
        self.événements: list[events.Event] = []

    def allouer(self, ligne: LigneDeCommande) -> str:
        """
        Alloue une ligne de commande au lot le plus approprié.

        Stratégie : sorted(self.lots) utilise __gt__ pour trier —
        les lots en stock (sans ETA) d'abord, puis par ETA croissante.

        Retourne la référence du lot choisi.
        Émet Alloué en cas de succès, RuptureDeStock sinon.
        """
        try:
            # next() + générateur filtrée : on prend le premier lot trié
            # qui peut accueillir la ligne
            lot = next(
                l for l in sorted(self.lots)
                if l.peut_allouer(ligne)
            )
        except StopIteration:
            self.événements.append(events.RuptureDeStock(sku=ligne.sku))
            return ""

        lot.allouer(ligne)
        self.numéro_version += 1
        self.événements.append(
            events.Alloué(
                id_commande=ligne.id_commande,
                sku=ligne.sku,
                quantité=ligne.quantité,
                réf_lot=lot.référence,
            )
        )
        return lot.référence

    def modifier_quantité_lot(self, réf: str, quantité: int) -> None:
        """
        Modifie la quantité d'un lot et réalloue si nécessaire.

        Si la nouvelle quantité est inférieure aux allocations existantes,
        les lignes en excès sont désallouées et des événements
        Désalloué sont émis pour chacune d'elles.
        """
        lot = next(l for l in self.lots if l.référence == réf)
        lot._quantité_achetée = quantité
        while lot.quantité_disponible < 0:
            ligne = lot.désallouer_une()
            self.événements.append(
                events.Désalloué(
                    id_commande=ligne.id_commande,
                    sku=ligne.sku,
                    quantité=ligne.quantité,
                )
            )
