# Chapitre 5 -- TDD à haute et basse vitesse

!!! info "Avant / Après"

    | | |
    |---|---|
    | **Avant** | Tests unitaires OU tests E2E coûteux |
    | **Après** | Stratégie 2 vitesses : haute (fakes) + basse (domaine pur) |

## Où en sommes-nous ?

Dans les chapitres précédents, nous avons construit un modèle de domaine (`Lot`, `LigneDeCommande`, `Produit`), un Repository pour le persister, et une Service Layer pour orchestrer les cas d'utilisation. Nous avons également écrit des tests à différents niveaux.

Mais une question reste ouverte : **à quel niveau faut-il écrire nos tests ?** Faut-il tester chaque méthode du domaine ? Passer systématiquement par la service layer ? Écrire des tests end-to-end pour tout ?

Ce chapitre propose un cadre de réflexion pour choisir le bon niveau de test selon la situation, en s'appuyant sur la métaphore de la **boîte de vitesses** : parfois on roule en haute vitesse (high gear), parfois en basse vitesse (low gear).

## La pyramide des tests

La pyramide des tests est un modèle classique qui guide la répartition de l'effort de test :

```
         /  E2E  \           <- Peu, lents, coûteux
        /----------\
       / Integration \       <- Nombre moyen
      /----------------\
     /   Unit Tests     \    <- Nombreux, rapides
    /____________________\
```

**Unit tests** forment la base. Ils sont rapides (millisecondes), isolés, et nombreux. Ils testent une unité de logique sans dépendance externe.

**Integration tests** vérifient que les composants fonctionnent ensemble : le repository avec une vraie base de données, l'API avec un vrai serveur HTTP.

**End-to-end tests** (E2E) traversent tout le système, du point d'entrée HTTP jusqu'à la base de données. Ils sont lents, fragiles, et coûteux à maintenir.

### Pourquoi cette forme pyramidale ?

La raison est économique. Plus un test est haut dans la pyramide :

- Plus il est **lent** à exécuter (secondes voire minutes contre millisecondes)
- Plus il est **fragile** face aux changements d'infrastructure
- Plus il est **difficile** à debugger quand il échoue
- Plus il couvre de code **par test**, mais avec moins de **précision**

À l'inverse, les unit tests du bas de la pyramide sont rapides, stables, et précis. C'est pourquoi on en veut un maximum.

!!! note "Règle empirique"
    Si votre suite de tests met plus de quelques secondes à s'exécuter, vous n'avez probablement pas assez de unit tests et trop de tests d'intégration.

## Tests "haute vitesse" (high gear)

Les tests **high gear** sont ceux qui passent par la **service layer**. Ils ne connaissent pas les détails internes du domaine. Ils envoient des commands et vérifient les résultats.

C'est notre fichier `tests/unit/test_handlers.py`, qui utilise les fakes définis aux chapitres précédents : le `FakeRepository` ([chapitre 2](chapitre_02_repository.md)) et le `FakeUnitOfWork` ([chapitre 6](chapitre_06_unit_of_work.md)). Ces fakes remplacent la base de données par de simples structures en mémoire. Les tests restent donc **rapides** (pas d'I/O) tout en traversant la logique réelle de la service layer et du domaine.

### Des tests qui expriment le "quoi"

Regardons les tests d'allocation :

```python
class TestAllouer:
    def test_allouer_retourne_la_référence_du_lot(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("b1", "CHAISE-COMFY", 100, None))
        results = bus.handle(commands.Allouer("o1", "CHAISE-COMFY", 10))

        assert results.pop(0) == "b1"

    def test_allouer_lève_sku_inconnu(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("b1", "VRAI-SKU", 100, None))

        with pytest.raises(handlers.SkuInconnu, match="SKU-INEXISTANT"):
            bus.handle(commands.Allouer("o1", "SKU-INEXISTANT", 10))
```

Ces tests ne savent pas **comment** l'allocation fonctionne en interne. Ils ne connaissent ni `Lot`, ni `LigneDeCommande`, ni la stratégie de tri. Ils envoient une command `Allouer` et vérifient le résultat.

C'est le **"quoi"** : *quand j'alloue la commande o1 pour le SKU CHAISE-COMFY, j'obtiens le lot b1*.

### Tests de changement de quantité

Le même principe s'applique aux scénarios plus complexes :

```python
class TestModifierQuantitéLot:
    def test_changes_quantité_disponible(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("b1", "TAPIS-ADORABLE", 100, None))
        [lot] = bus.uow.produits.get("TAPIS-ADORABLE").lots
        assert lot.quantité_disponible == 100

        bus.handle(commands.ModifierQuantitéLot("b1", 50))
        assert lot.quantité_disponible == 50

    def test_réalloue_si_nécessaire(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("b1", "TASSE-INDIGO", 50, None))
        bus.handle(commands.Allouer("o1", "TASSE-INDIGO", 20))
        bus.handle(commands.Allouer("o2", "TASSE-INDIGO", 20))

        bus.handle(commands.CréerLot("b2", "TASSE-INDIGO", 100, None))
        bus.handle(commands.ModifierQuantitéLot("b1", 25))

        # L'une des lignes a été réallouée au nouveau lot
        assert bus.uow.produits.get("TASSE-INDIGO").lots[0].quantité_disponible == 5
```

Le test `test_réalloue_si_nécessaire` vérifie un scénario métier complet : quand on réduit la quantité d'un lot en dessous de ses allocations, le système doit automatiquement réallouer les lignes en excès vers un autre lot. On ne teste pas le mécanisme interne de réallocation, on vérifie que **le résultat final est correct**.

### Avantages des tests high gear

- **Rapides** : grâce aux fakes, pas d'I/O
- **Stables** : ils ne cassent pas quand on refactore le domaine
- **Expressifs** : ils décrivent les cas d'utilisation métier
- **Bonne couverture** : chaque test traverse service layer + domaine

## Tests "basse vitesse" (low gear)

Les tests **low gear** sont les tests unitaires du modèle de domaine. Ils travaillent directement avec les objets `Lot`, `LigneDeCommande`, et `Produit`.

C'est notre fichier `tests/unit/test_model.py` :

```python
def créer_lot_et_ligne(
    sku: str, quantité_lot: int, quantité_ligne: int
) -> tuple[Lot, LigneDeCommande]:
    return (
        Lot("lot-001", sku, quantité_lot, eta=date.today()),
        LigneDeCommande("commande-ref", sku, quantité_ligne),
    )
```

### Tests granulaires du Lot

```python
class TestLot:
    def test_allouer_réduit_la_quantité_disponible(self):
        lot, ligne = créer_lot_et_ligne("PETITE-TABLE", 20, 2)
        lot.allouer(ligne)
        assert lot.quantité_disponible == 18

    def test_peut_allouer_si_disponible_supérieur(self):
        lot, ligne = créer_lot_et_ligne("ELEGANTE-LAMPE", 20, 2)
        assert lot.peut_allouer(ligne)

    def test_ne_peut_pas_allouer_si_disponible_insuffisant(self):
        lot, ligne = créer_lot_et_ligne("ELEGANTE-LAMPE", 2, 20)
        assert not lot.peut_allouer(ligne)

    def test_allocation_idempotente(self):
        lot, ligne = créer_lot_et_ligne("ANGULAR-DESK", 20, 2)
        lot.allouer(ligne)
        lot.allouer(ligne)
        assert lot.quantité_disponible == 18
```

Ces tests sont **très proches de l'implémentation**. Ils vérifient directement les méthodes `allouer()`, `peut_allouer()`, et `désallouer()` de la classe `Lot`. Ils testent le **"comment"** de la logique d'allocation.

### Tests de la stratégie d'allocation dans Produit

```python
class TestProduit:
    def test_préfère_les_lots_en_stock_aux_livraisons(self):
        lot_en_stock = Lot("lot-stock", "HORLOGE-RETRO", 100, eta=None)
        lot_en_transit = Lot(
            "lot-transit", "HORLOGE-RETRO", 100,
            eta=date.today() + timedelta(days=1)
        )
        produit = Produit(
            sku="HORLOGE-RETRO",
            lots=[lot_en_stock, lot_en_transit]
        )
        ligne = LigneDeCommande("oref", "HORLOGE-RETRO", 10)

        produit.allouer(ligne)

        assert lot_en_stock.quantité_disponible == 90
        assert lot_en_transit.quantité_disponible == 100

    def test_préfère_les_lots_avec_eta_la_plus_proche(self):
        plus_tôt = Lot("lot-rapide", "LAMPE-MINIMALE", 100, eta=date.today())
        moyen = Lot(
            "lot-normal", "LAMPE-MINIMALE", 100,
            eta=date.today() + timedelta(days=5)
        )
        plus_tard = Lot(
            "lot-lent", "LAMPE-MINIMALE", 100,
            eta=date.today() + timedelta(days=10)
        )
        produit = Produit(
            sku="LAMPE-MINIMALE", lots=[moyen, plus_tôt, plus_tard]
        )
        ligne = LigneDeCommande("order1", "LAMPE-MINIMALE", 10)

        produit.allouer(ligne)

        assert plus_tôt.quantité_disponible == 90
        assert moyen.quantité_disponible == 100
        assert plus_tard.quantité_disponible == 100
```

Ces tests vérifient la **règle métier précise** : les lots en stock sont préférés aux livraisons, et parmi les livraisons, la plus proche en date l'emporte. Ils sont indispensables pour **développer** cette logique, car ils donnent un feedback immédiat et précis.

### Quand les tests low gear brillent

Les tests low gear sont particulièrement utiles quand :

- On **développe une nouvelle règle métier** et on a besoin de feedback rapide et précis
- La logique est **complexe** (algorithmes de tri, calculs, règles conditionnelles)
- On veut **documenter** le comportement attendu d'une entité ou d'un value object
- On débogue un **cas limite** spécifique

## Quand utiliser quel niveau ?

La métaphore de la boîte de vitesses est éclairante. Quand on démarre un projet ou une nouvelle fonctionnalité :

### Phase 1 : Basse vitesse (low gear) -- Développer la logique

On commence par des **tests du domaine** pour construire la logique métier pas à pas.

```python
# On développe la règle d'allocation lot par lot
def test_allouer_réduit_la_quantité_disponible(self):
    lot, ligne = créer_lot_et_ligne("PETITE-TABLE", 20, 2)
    lot.allouer(ligne)
    assert lot.quantité_disponible == 18
```

À ce stade, on avance lentement mais avec précision. Chaque test vérifie un aspect spécifique du modèle. C'est le moment de la **découverte** : on explore le domaine, on affine les règles, on ajuste les abstractions.

### Phase 2 : Haute vitesse (high gear) -- Stabiliser et protéger

Une fois la logique en place, on **remonte** vers la service layer pour écrire les tests de non-régression :

```python
# On vérifie le cas d'utilisation complet
def test_allouer_retourne_la_référence_du_lot(self):
    bus = bootstrap_test_bus()
    bus.handle(commands.CréerLot("b1", "CHAISE-COMFY", 100, None))
    results = bus.handle(commands.Allouer("o1", "CHAISE-COMFY", 10))
    assert results.pop(0) == "b1"
```

Ces tests sont plus stables dans le temps. Si on décide de refactorer le modèle de domaine (changer la structure interne de `Lot`, réorganiser `Produit`), les tests high gear continuent de passer tant que le **comportement externe** reste le même.

### Le bon ratio

En pratique, une base de code mature tend vers cette répartition :

- **Beaucoup de tests high gear** : ils couvrent tous les cas d'utilisation et servent de filet de sécurité pour le refactoring
- **Quelques tests low gear** : ils documentent les règles métier complexes et les cas limites du domaine
- **Très peu de tests E2E** : ils vérifient que le système fonctionne de bout en bout (API -> BDD -> réponse)

!!! tip "Conseil pratique"
    Si vous pouvez supprimer un test low gear sans perdre de couverture fonctionnelle (parce qu'un test high gear couvre déjà le même scénario), c'est probablement un bon candidat à la suppression. Moins de tests à maintenir, c'est moins de friction lors du refactoring.

## Le piège des tests couplés à l'implémentation

Le plus grand danger en matière de tests, c'est d'écrire des tests qui vérifient **comment** le code fonctionne plutôt que **ce qu'il fait**. Ces tests sont fragiles : ils cassent dès qu'on refactore, même si le comportement reste identique.

### Exemple de test fragile

Imaginons un test qui vérifie les détails internes :

```python
# MAUVAIS : couplage à l'implémentation
def test_allocation_adds_to_internal_set():
    lot = Lot("b1", "SKU-001", 100)
    ligne = LigneDeCommande("o1", "SKU-001", 10)
    lot.allouer(ligne)

    # On vérifie la structure interne !
    assert ligne in lot._allocations
    assert len(lot._allocations) == 1
```

Ce test accède à `_allocations`, un attribut privé. Si on décide de remplacer le `set` par une `list`, ou de renommer l'attribut, le test casse -- alors que le comportement n'a pas changé.

### Le même test, orienté comportement

```python
# BON : on teste le comportement observable
def test_allouer_réduit_la_quantité_disponible():
    lot, ligne = créer_lot_et_ligne("PETITE-TABLE", 20, 2)
    lot.allouer(ligne)
    assert lot.quantité_disponible == 18
```

Ce test vérifie le **résultat observable** : après une allocation, la quantité disponible diminue. Peu importe comment c'est implémenté en interne.

### Symptômes de tests couplés à l'implémentation

Voici les signes d'alerte :

- Le test accède à des **attributs privés** (`_allocations`, `_quantité_achetée`)
- Le test vérifie des **appels de méthodes** avec `mock.assert_called_with()`
- Le test **casse quand on refactore** sans changer le comportement
- Le test est **difficile à lire** car il reproduit la logique interne

### La règle d'or

> **Testez les inputs et les outputs, pas les mécanismes internes.**

Pour un test high gear, les inputs sont des **commands** et les outputs sont les **effets observables** (valeurs de retour, état du repository, events émis).

Pour un test low gear, les inputs sont des **appels de méthodes** sur le domaine et les outputs sont les **propriétés publiques** (`quantité_disponible`, `référence`, etc.).

## Exercices

!!! example "Exercice 1 -- Classer vos tests"
    Prenez un projet existant (le vôtre ou un projet open source). Classez chaque test en "low gear" ou "high gear". Quel est le ratio ? Est-il conforme à la pyramide ?

!!! example "Exercice 2 -- Refactoring sans casser les tests"
    Dans le modèle de domaine, remplacez le `set` `_allocations` de `Lot` par une `list` (en gérant l'unicité manuellement). Les tests high gear doivent-ils changer ? Et les tests low gear ?

!!! example "Exercice 3 -- Supprimer un test redondant"
    Identifiez un test low gear qui est déjà couvert par un test high gear. Supprimez-le et vérifiez que la couverture fonctionnelle n'a pas diminué. Êtes-vous à l'aise avec cette suppression ? Pourquoi ?

---

## Résumé

### Principe général

Le TDD à deux vitesses nous invite à **adapter notre niveau de test à la phase de développement**. En basse vitesse, on construit et on explore. En haute vitesse, on protège et on stabilise.

### Tableau comparatif

| Critère | Low gear (domaine) | High gear (service layer) | E2E |
|---------|-------------------|--------------------------|-----|
| **Fichier** | `test_model.py` | `test_handlers.py` | `test_api.py` |
| **Vitesse** | Très rapide | Rapide (avec fakes) | Lent |
| **Granularité** | Fine (une méthode) | Moyenne (un use case) | Large (tout le système) |
| **Stabilité** | Fragile si le domaine change | Stable tant que le comportement tient | Fragile (infra) |
| **Quand l'utiliser** | Développer une règle métier | Tests de non-régression | Smoke tests |
| **Ce qu'on teste** | Le "comment" du domaine | Le "quoi" du use case | Le système entier |
| **Dépendances** | Aucune | Fakes (FakeRepository, FakeUoW) | Vraie BDD, vrai serveur |

### Points clés à retenir

- **La pyramide des tests** guide la répartition : beaucoup de unit tests, peu de E2E
- **Les tests high gear** passent par la service layer avec des fakes ; ils sont stables et couvrent les use cases
- **Les tests low gear** testent le domaine directement ; ils sont utiles pour développer la logique
- **Commencez en low gear** pour développer une règle, puis **remontez en high gear** pour la non-régression
- **Évitez le couplage à l'implémentation** : testez le comportement (inputs/outputs), pas la mécanique interne
- **Moins de tests, mieux ciblés** vaut mieux qu'une couverture exhaustive de chaque détail d'implémentation
