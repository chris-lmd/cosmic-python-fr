# Chapitre 5 -- TDD à haute et basse vitesse

## Où en sommes-nous ?

Dans les chapitres précédents, nous avons construit un modèle de domaine (`Batch`, `OrderLine`, `Product`), un Repository pour le persister, et une Service Layer pour orchestrer les cas d'utilisation. Nous avons également écrit des tests à différents niveaux.

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

C'est notre fichier `tests/unit/test_handlers.py` :

```python
class FakeRepository(AbstractRepository):
    """Fake repository qui stocke les produits en mémoire."""

    def __init__(self, products: list[model.Product] | None = None):
        super().__init__()
        self._products = set(products or [])

    def _add(self, product: model.Product) -> None:
        self._products.add(product)

    def _get(self, sku: str) -> model.Product | None:
        return next((p for p in self._products if p.sku == sku), None)
```

```python
class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    """Fake Unit of Work utilisant le FakeRepository."""

    def __init__(self):
        self.products = FakeRepository([])
        self.committed = False

    def _commit(self):
        self.committed = True

    def rollback(self):
        pass
```

Ces fakes remplacent la base de données par de simples structures en mémoire. Les tests restent donc **rapides** (pas d'I/O) tout en traversant la logique réelle de la service layer et du domaine.

### Des tests qui expriment le "quoi"

Regardons les tests d'allocation :

```python
class TestAllocate:
    def test_allocate_returns_batch_ref(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "CHAISE-COMFY", 100, None))
        results = bus.handle(commands.Allocate("o1", "CHAISE-COMFY", 10))

        assert results.pop(0) == "b1"

    def test_allocate_errors_for_invalid_sku(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "VRAI-SKU", 100, None))

        with pytest.raises(handlers.InvalidSku, match="SKU-INEXISTANT"):
            bus.handle(commands.Allocate("o1", "SKU-INEXISTANT", 10))
```

Ces tests ne savent pas **comment** l'allocation fonctionne en interne. Ils ne connaissent ni `Batch`, ni `OrderLine`, ni la stratégie de tri. Ils envoient une command `Allocate` et vérifient le résultat.

C'est le **"quoi"** : *quand j'alloue la commande o1 pour le SKU CHAISE-COMFY, j'obtiens le batch b1*.

### Tests de changement de quantité

Le même principe s'applique aux scénarios plus complexes :

```python
class TestChangeBatchQuantity:
    def test_changes_available_quantity(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "TAPIS-ADORABLE", 100, None))
        [batch] = bus.uow.products.get("TAPIS-ADORABLE").batches
        assert batch.available_quantity == 100

        bus.handle(commands.ChangeBatchQuantity("b1", 50))
        assert batch.available_quantity == 50

    def test_reallocates_if_necessary(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "TASSE-INDIGO", 50, None))
        bus.handle(commands.Allocate("o1", "TASSE-INDIGO", 20))
        bus.handle(commands.Allocate("o2", "TASSE-INDIGO", 20))

        bus.handle(commands.CreateBatch("b2", "TASSE-INDIGO", 100, None))
        bus.handle(commands.ChangeBatchQuantity("b1", 25))

        # L'une des lignes a été réallouée au nouveau batch
        assert bus.uow.products.get("TASSE-INDIGO").batches[0].available_quantity == 5
```

Le test `test_reallocates_if_necessary` vérifie un scénario métier complet : quand on réduit la quantité d'un batch en dessous de ses allocations, le système doit automatiquement réallouer les lignes en excès vers un autre batch. On ne teste pas le mécanisme interne de réallocation, on vérifie que **le résultat final est correct**.

### Avantages des tests high gear

- **Rapides** : grâce aux fakes, pas d'I/O
- **Stables** : ils ne cassent pas quand on refactore le domaine
- **Expressifs** : ils décrivent les cas d'utilisation métier
- **Bonne couverture** : chaque test traverse service layer + domaine

## Tests "basse vitesse" (low gear)

Les tests **low gear** sont les tests unitaires du modèle de domaine. Ils travaillent directement avec les objets `Batch`, `OrderLine`, et `Product`.

C'est notre fichier `tests/unit/test_model.py` :

```python
def make_batch_and_line(
    sku: str, batch_qty: int, line_qty: int
) -> tuple[Batch, OrderLine]:
    return (
        Batch("batch-001", sku, batch_qty, eta=date.today()),
        OrderLine("order-ref", sku, line_qty),
    )
```

### Tests granulaires du Batch

```python
class TestBatch:
    def test_allocating_reduces_available_quantity(self):
        batch, line = make_batch_and_line("PETITE-TABLE", 20, 2)
        batch.allocate(line)
        assert batch.available_quantity == 18

    def test_can_allocate_if_available_greater_than_required(self):
        batch, line = make_batch_and_line("ELEGANTE-LAMPE", 20, 2)
        assert batch.can_allocate(line)

    def test_cannot_allocate_if_available_smaller_than_required(self):
        batch, line = make_batch_and_line("ELEGANTE-LAMPE", 2, 20)
        assert not batch.can_allocate(line)

    def test_allocation_is_idempotent(self):
        batch, line = make_batch_and_line("ANGULAR-DESK", 20, 2)
        batch.allocate(line)
        batch.allocate(line)
        assert batch.available_quantity == 18
```

Ces tests sont **très proches de l'implémentation**. Ils vérifient directement les méthodes `allocate`, `can_allocate`, et `deallocate` de la classe `Batch`. Ils testent le **"comment"** de la logique d'allocation.

### Tests de la stratégie d'allocation dans Product

```python
class TestProduct:
    def test_prefers_warehouse_batches_to_shipments(self):
        in_stock_batch = Batch("in-stock-batch", "HORLOGE-RETRO", 100, eta=None)
        shipment_batch = Batch(
            "shipment-batch", "HORLOGE-RETRO", 100,
            eta=date.today() + timedelta(days=1)
        )
        product = Product(
            sku="HORLOGE-RETRO",
            batches=[in_stock_batch, shipment_batch]
        )
        line = OrderLine("oref", "HORLOGE-RETRO", 10)

        product.allocate(line)

        assert in_stock_batch.available_quantity == 90
        assert shipment_batch.available_quantity == 100

    def test_prefers_earlier_batches(self):
        earliest = Batch("speedy-batch", "LAMPE-MINIMALE", 100, eta=date.today())
        medium = Batch(
            "normal-batch", "LAMPE-MINIMALE", 100,
            eta=date.today() + timedelta(days=5)
        )
        latest = Batch(
            "slow-batch", "LAMPE-MINIMALE", 100,
            eta=date.today() + timedelta(days=10)
        )
        product = Product(
            sku="LAMPE-MINIMALE", batches=[medium, earliest, latest]
        )
        line = OrderLine("order1", "LAMPE-MINIMALE", 10)

        product.allocate(line)

        assert earliest.available_quantity == 90
        assert medium.available_quantity == 100
        assert latest.available_quantity == 100
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
# On développe la règle d'allocation batch par batch
def test_allocating_reduces_available_quantity(self):
    batch, line = make_batch_and_line("PETITE-TABLE", 20, 2)
    batch.allocate(line)
    assert batch.available_quantity == 18
```

À ce stade, on avance lentement mais avec précision. Chaque test vérifie un aspect spécifique du modèle. C'est le moment de la **découverte** : on explore le domaine, on affine les règles, on ajuste les abstractions.

### Phase 2 : Haute vitesse (high gear) -- Stabiliser et protéger

Une fois la logique en place, on **remonte** vers la service layer pour écrire les tests de non-régression :

```python
# On vérifie le cas d'utilisation complet
def test_allocate_returns_batch_ref(self):
    bus = bootstrap_test_bus()
    bus.handle(commands.CreateBatch("b1", "CHAISE-COMFY", 100, None))
    results = bus.handle(commands.Allocate("o1", "CHAISE-COMFY", 10))
    assert results.pop(0) == "b1"
```

Ces tests sont plus stables dans le temps. Si on décide de refactorer le modèle de domaine (changer la structure interne de `Batch`, réorganiser `Product`), les tests high gear continuent de passer tant que le **comportement externe** reste le même.

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
    batch = Batch("b1", "SKU-001", 100)
    line = OrderLine("o1", "SKU-001", 10)
    batch.allocate(line)

    # On vérifie la structure interne !
    assert line in batch._allocations
    assert len(batch._allocations) == 1
```

Ce test accède à `_allocations`, un attribut privé. Si on décide de remplacer le `set` par une `list`, ou de renommer l'attribut, le test casse -- alors que le comportement n'a pas changé.

### Le même test, orienté comportement

```python
# BON : on teste le comportement observable
def test_allocating_reduces_available_quantity():
    batch, line = make_batch_and_line("PETITE-TABLE", 20, 2)
    batch.allocate(line)
    assert batch.available_quantity == 18
```

Ce test vérifie le **résultat observable** : après une allocation, la quantité disponible diminue. Peu importe comment c'est implémenté en interne.

### Symptômes de tests couplés à l'implémentation

Voici les signes d'alerte :

- Le test accède à des **attributs privés** (`_allocations`, `_purchased_quantity`)
- Le test vérifie des **appels de méthodes** avec `mock.assert_called_with()`
- Le test **casse quand on refactore** sans changer le comportement
- Le test est **difficile à lire** car il reproduit la logique interne

### La règle d'or

> **Testez les inputs et les outputs, pas les mécanismes internes.**

Pour un test high gear, les inputs sont des **commands** et les outputs sont les **effets observables** (valeurs de retour, état du repository, events émis).

Pour un test low gear, les inputs sont des **appels de méthodes** sur le domaine et les outputs sont les **propriétés publiques** (`available_quantity`, `reference`, etc.).

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
