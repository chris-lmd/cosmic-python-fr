# Chapitre 1 -- Le Domain Model

## Pourquoi un modèle de domaine ?

Imaginons un système d'allocation de stock. Un client passe une commande, et le système doit décider quel lot de marchandise utiliser pour honorer cette commande. Simple en apparence, mais les règles s'accumulent vite : on préfère puiser dans le stock déjà en entrepôt plutôt que dans une livraison à venir, on choisit la livraison la plus proche si tout le stock en entrepôt est épuisé, on ne peut pas allouer plus que ce qui est disponible, on ne peut pas allouer un SKU différent de celui commandé...

Dans beaucoup de projets, cette logique finit dispersée un peu partout : dans les vues Django, dans les endpoints FastAPI, dans des scripts SQL. C'est ce qu'on appelle parfois un **transaction script** -- un gros bloc procédural qui mélange logique métier, accès aux données et orchestration. Ça fonctionne au début, puis ça devient un cauchemar à maintenir et à tester.

Le **Domain Model** est une réponse à ce problème. L'idée est de concentrer toute la logique métier dans une couche de code pur Python, sans aucune dépendance technique. Pas de base de données, pas de framework web, pas d'import `requests` ou `sqlalchemy`. Juste des classes, des méthodes et des règles métier.

!!! tip "L'avantage principal"
    Un modèle de domaine pur se teste avec de simples tests unitaires, sans fixtures de base de données ni serveur HTTP. Les tests s'exécutent en millisecondes.

## Qu'est-ce qu'un Domain Model ?

Le Domain Model est une représentation en code des concepts, des règles et des processus du domaine métier. C'est la traduction directe de ce que les experts métier décrivent quand ils parlent de leur travail.

Dans notre cas, les experts métier parlent de **lignes de commande**, de **lots de stock**, de **SKU** (Stock Keeping Unit), d'**allocation** et de **quantité disponible**. Le Domain Model reprend exactement ce vocabulaire.

```
Vocabulaire metier          Code
-----------------          ----
Ligne de commande    -->   OrderLine
Lot de stock         -->   Batch
Allouer              -->   allocate()
Quantite disponible  -->   available_quantity
Reference produit    -->   SKU (str)
```

La distinction fondamentale avec un transaction script, c'est l'endroit où vivent les règles. Dans un transaction script, la logique est dans le handler :

```python
# Transaction script -- a eviter
def allocate(order_id, sku, qty, session):
    batches = session.query(Batch).filter_by(sku=sku).all()
    batches.sort(key=lambda b: (b.eta is not None, b.eta))
    for batch in batches:
        if batch._purchased_quantity - batch.allocated_qty >= qty:
            batch.allocated_qty += qty
            session.commit()
            return batch.reference
    raise OutOfStock(sku)
```

Dans un Domain Model, la logique vit dans les objets du domaine eux-mêmes. Le handler ne fait que les orchestrer. C'est cette séparation qui rend le code testable, lisible et maintenable.

## Value Objects

Un **Value Object** est un objet défini par ses attributs, pas par une identité. Deux billets de 10 euros sont interchangeables : peu importe lequel vous avez, ce qui compte c'est la valeur. De la même façon, deux lignes de commande avec le même `orderid`, le même `sku` et la même `qty` sont identiques.

Voici notre Value Object `OrderLine` :

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class OrderLine:
    """
    Value Object représentant une ligne de commande.

    Un value object est immuable et défini par ses attributs,
    pas par une identité. Deux OrderLine avec les mêmes attributs
    sont considérées comme identiques.
    """

    orderid: str
    sku: str
    qty: int
```

Le décorateur `@dataclass(frozen=True)` fait deux choses essentielles :

1. **Immutabilité** -- On ne peut pas modifier les attributs après création. Un `line.qty = 5` lèvera une `FrozenInstanceError`. C'est voulu : un Value Object ne change pas, on en crée un nouveau si besoin.

2. **Hashabilité** -- Un objet `frozen` est automatiquement hashable, ce qui permet de l'utiliser dans des `set` et comme clé de `dict`. C'est indispensable pour notre modèle, car `Batch` stocke ses allocations dans un `set[OrderLine]`.

??? note "Pourquoi `@dataclass` et pas `NamedTuple` ?"
    Les deux sont des choix valables. `@dataclass(frozen=True)` offre un peu plus de flexibilité (héritage, méthodes, valeurs par défaut mutables via `field`). `NamedTuple` est légèrement plus performant en mémoire. Pour un Domain Model, la différence est négligeable. L'important, c'est l'immutabilité et l'égalité structurelle.

On peut vérifier le comportement d'égalité :

```python
def test_equality():
    """Deux OrderLine avec les mêmes attributs sont égales (value object)."""
    line1 = OrderLine("order1", "SKU-001", 10)
    line2 = OrderLine("order1", "SKU-001", 10)
    assert line1 == line2

def test_inequality():
    line1 = OrderLine("order1", "SKU-001", 10)
    line2 = OrderLine("order2", "SKU-001", 10)
    assert line1 != line2
```

Pas besoin d'écrire `__eq__` : `@dataclass` le génère automatiquement en comparant tous les attributs.

## Entities

Une **Entity** est un objet avec une identité qui persiste dans le temps. Même si ses attributs changent, l'entité reste la même. Un lot de stock avec la référence `"batch-042"` reste le même lot, qu'il contienne 100 ou 50 unités.

Voici notre entité `Batch` :

```python
class Batch:
    """
    Entité représentant un lot de stock.

    Un Batch a une identité (sa référence) et un cycle de vie.
    Il contient une quantité de stock pour un SKU donné,
    avec une date d'arrivée (ETA) optionnelle.
    """

    def __init__(self, ref: str, sku: str, qty: int, eta: Optional[date] = None):
        self.reference = ref
        self.sku = sku
        self.eta = eta
        self._purchased_quantity = qty
        self._allocations: set[OrderLine] = set()

    def __repr__(self) -> str:
        return f"<Batch {self.reference}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Batch):
            return NotImplemented
        return self.reference == other.reference

    def __hash__(self) -> int:
        return hash(self.reference)
```

Trois points importants :

**`__eq__` compare uniquement la référence.** Deux objets `Batch` avec la même référence sont considérés comme la même entité, peu importe les autres attributs. C'est la définition même d'une entité : l'égalité est basée sur l'identité, pas sur la valeur.

**`__hash__` est basé sur la référence.** Quand on redéfinit `__eq__`, Python rend l'objet non-hashable par défaut. On doit donc redéfinir `__hash__` explicitement, en se basant sur le même attribut que `__eq__`.

**`NotImplemented` plutôt que `False`.** Quand on compare un `Batch` avec un objet d'un autre type, on retourne `NotImplemented` pour laisser Python essayer l'autre opérande. C'est une bonne pratique souvent oubliée.

!!! warning "Entity vs Value Object : la règle"
    Si deux objets avec les mêmes attributs sont interchangeables, c'est un Value Object. Si un objet a un cycle de vie et une identité qui persiste même quand ses attributs changent, c'est une Entity.

## Les règles métier dans le domaine

C'est ici que le Domain Model prend tout son sens. Les règles métier ne sont pas dans un service externe ou dans un handler : elles vivent directement dans les objets du domaine.

### Vérifier qu'on peut allouer

```python
def can_allocate(self, line: OrderLine) -> bool:
    """Vérifie si ce lot peut accueillir la ligne de commande."""
    return self.sku == line.sku and self.available_quantity >= line.qty
```

Deux conditions, et elles se lisent comme du langage naturel : le SKU doit correspondre, et la quantité disponible doit être suffisante.

### Allouer une ligne de commande

```python
def allocate(self, line: OrderLine) -> None:
    """Alloue une ligne de commande à ce lot."""
    if self.can_allocate(line):
        self._allocations.add(line)
```

L'allocation revient à ajouter la ligne de commande dans l'ensemble `_allocations`. Comme `OrderLine` est un Value Object hashable, le `set` garantit l'idempotence : allouer deux fois la même ligne n'a aucun effet.

### Désallouer

```python
def deallocate(self, line: OrderLine) -> None:
    """Désalloue une ligne de commande de ce lot."""
    if line in self._allocations:
        self._allocations.discard(line)
```

La désallocation est l'opération inverse. On utilise `discard` plutôt que `remove` pour éviter une exception si la ligne n'est pas présente, mais la vérification `if line in self._allocations` rend l'intention explicite.

### Quantités calculées

```python
@property
def allocated_quantity(self) -> int:
    return sum(line.qty for line in self._allocations)

@property
def available_quantity(self) -> int:
    return self._purchased_quantity - self.allocated_quantity
```

La quantité disponible est toujours calculée à partir de l'état réel des allocations. Pas de compteur à maintenir manuellement, pas de risque de désynchronisation. C'est un choix de conception délibéré : on préfère recalculer plutôt que de maintenir un état dérivé.

??? note "Performance"
    Recalculer `available_quantity` à chaque accès peut sembler coûteux. En pratique, un lot a rarement plus de quelques dizaines d'allocations. Si la performance devenait un problème, on pourrait ajouter un cache -- mais pas avant d'avoir mesuré. L'optimisation prématurée est l'ennemi du code clair.

## La stratégie d'allocation

Quand un client passe une commande, on veut allouer depuis le lot le plus pertinent. La règle métier est :

1. **D'abord les lots en stock** (ceux qui sont déjà en entrepôt, sans ETA).
2. **Puis les livraisons par ETA croissante** (la plus proche d'abord).

Pour implémenter cette stratégie, on définit `__gt__` sur `Batch` :

```python
def __gt__(self, other: Batch) -> bool:
    if self.eta is None:
        return False
    if other.eta is None:
        return True
    return self.eta > other.eta
```

La logique est la suivante :

- Un lot **sans ETA** (en stock) n'est jamais "plus grand" qu'un autre. Il sera donc toujours trié en premier.
- Un lot **avec ETA** est toujours "plus grand" qu'un lot sans ETA.
- Entre deux lots avec ETA, le tri se fait par date.

Cela permet d'utiliser simplement `sorted()` pour obtenir les lots dans l'ordre de préférence :

```python
class Product:
    def allocate(self, line: OrderLine) -> str:
        """
        Alloue une ligne de commande au lot le plus approprié.

        La stratégie d'allocation privilégie les lots en stock
        (sans ETA) puis les lots avec l'ETA la plus proche.
        """
        try:
            batch = next(
                b for b in sorted(self.batches)
                if b.can_allocate(line)
            )
        except StopIteration:
            self.events.append(events.OutOfStock(sku=line.sku))
            return ""

        batch.allocate(line)
        self.version_number += 1
        return batch.reference
```

`sorted(self.batches)` trie les lots grâce à `__gt__`. Puis on prend le premier qui peut accueillir la ligne (`can_allocate`). Si aucun lot ne convient, on émet un événement `OutOfStock`.

!!! tip "Pourquoi `__gt__` et pas `__lt__` ?"
    Python a besoin d'un seul opérateur de comparaison pour que `sorted()` fonctionne. On aurait pu définir `__lt__` à la place, avec la logique inversée. Le choix de `__gt__` est une convention : on considère que les lots les "plus grands" sont ceux qui arrivent le plus tard, ce qui est naturel quand on pense aux dates.

## Tester le modèle de domaine

L'avantage majeur d'un Domain Model pur, c'est la testabilité. Les tests sont simples, rapides et ne nécessitent aucune infrastructure.

### Tests du Batch

```python
from datetime import date, timedelta
from allocation.domain.model import Batch, OrderLine, Product


def make_batch_and_line(
    sku: str, batch_qty: int, line_qty: int
) -> tuple[Batch, OrderLine]:
    return (
        Batch("batch-001", sku, batch_qty, eta=date.today()),
        OrderLine("order-ref", sku, line_qty),
    )


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

    def test_cannot_allocate_if_skus_do_not_match(self):
        batch = Batch("batch-001", "CHAISE-INCOMFORTABLE", 100, eta=None)
        line = OrderLine("order-ref", "COUSSIN-MOELLEUX", 10)
        assert not batch.can_allocate(line)

    def test_allocation_is_idempotent(self):
        batch, line = make_batch_and_line("ANGULAR-DESK", 20, 2)
        batch.allocate(line)
        batch.allocate(line)
        assert batch.available_quantity == 18

    def test_deallocate(self):
        batch, line = make_batch_and_line("ANGULAR-DESK", 20, 2)
        batch.allocate(line)
        batch.deallocate(line)
        assert batch.available_quantity == 20
```

Remarquez la structure : chaque test crée ses objets, exécute une action et vérifie le résultat. Pas de `setUp` complexe, pas de mock, pas de base de données. Les noms des tests décrivent le comportement attendu en langage naturel.

### Tests de la stratégie d'allocation

```python
class TestProduct:
    def test_prefers_warehouse_batches_to_shipments(self):
        """Les lots en stock (sans ETA) sont préférés aux livraisons."""
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
        """Parmi les livraisons, on préfère la plus proche."""
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
            sku="LAMPE-MINIMALE",
            batches=[medium, earliest, latest]
        )
        line = OrderLine("order1", "LAMPE-MINIMALE", 10)

        product.allocate(line)

        assert earliest.available_quantity == 90
        assert medium.available_quantity == 100
        assert latest.available_quantity == 100
```

Le test `test_prefers_warehouse_batches_to_shipments` passe les lots dans l'ordre inverse (le lot en livraison avant celui en stock) pour vérifier que le tri fonctionne. Le test `test_prefers_earlier_batches` mélange volontairement l'ordre (`medium, earliest, latest`) pour la même raison.

Ces tests s'exécutent en quelques millisecondes. On peut en avoir des centaines sans que la suite de tests ne ralentisse. C'est un avantage considérable par rapport aux tests d'intégration qui nécessitent une base de données.

## Résumé

### Les concepts clés

| Concept | Description | Exemple |
|---------|-------------|---------|
| **Domain Model** | Couche de code pur qui représente les règles métier, sans dépendance technique. | Le module `model.py` |
| **Value Object** | Objet défini par ses attributs, immuable, sans identité propre. | `OrderLine` |
| **Entity** | Objet avec une identité persistante, même si ses attributs changent. | `Batch` |
| **Aggregate** | Entité racine qui garantit la cohérence d'un groupe d'objets. | `Product` |

### Avantages du pattern

- **Testabilité** -- La logique métier se teste en isolation, sans infrastructure. Les tests sont rapides et fiables.
- **Lisibilité** -- Le code du domaine utilise le vocabulaire métier. Un expert non-technique peut le relire et vérifier les règles.
- **Maintenabilité** -- Les règles métier sont centralisées. Quand une règle change, on sait exactement où intervenir.
- **Indépendance technique** -- Le domaine ne dépend pas de la base de données ni du framework web. On peut changer d'ORM ou de framework sans toucher aux règles métier.

### Inconvénients du pattern

- **Complexité initiale** -- Pour des CRUD simples, un Domain Model est excessif. Un transaction script suffit.
- **Mapping objet-relationnel** -- Le domaine étant découplé de la persistance, il faut une couche de mapping (c'est le sujet du prochain chapitre sur le Repository pattern).
- **Courbe d'apprentissage** -- Les concepts de DDD (Entity, Value Object, Aggregate) demandent un investissement initial.

!!! tip "Quand utiliser ce pattern ?"
    Le Domain Model vaut l'investissement quand la logique métier est complexe et susceptible d'évoluer. Si votre application est essentiellement un CRUD avec peu de règles métier, un transaction script ou un framework comme Django avec ses modèles "fat" sera plus adapté. Il n'y a pas de honte à choisir la simplicité quand elle suffit.

---

*Dans le prochain chapitre, nous verrons comment persister ce modèle de domaine sans le contaminer avec des détails techniques, grâce au pattern **Repository**.*
