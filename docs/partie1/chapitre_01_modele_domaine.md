# Chapitre 1 -- Le Domain Model

## Pourquoi un modele de domaine ?

Imaginons un systeme d'allocation de stock. Un client passe une commande, et le systeme doit decider quel lot de marchandise utiliser pour honorer cette commande. Simple en apparence, mais les regles s'accumulent vite : on prefere puiser dans le stock deja en entrepot plutot que dans une livraison a venir, on choisit la livraison la plus proche si tout le stock en entrepot est epuise, on ne peut pas allouer plus que ce qui est disponible, on ne peut pas allouer un SKU different de celui commande...

Dans beaucoup de projets, cette logique finit dispersee un peu partout : dans les vues Django, dans les endpoints FastAPI, dans des scripts SQL. C'est ce qu'on appelle parfois un **transaction script** -- un gros bloc procedural qui melange logique metier, acces aux donnees et orchestration. Ca fonctionne au debut, puis ca devient un cauchemar a maintenir et a tester.

Le **Domain Model** est une reponse a ce probleme. L'idee est de concentrer toute la logique metier dans une couche de code pur Python, sans aucune dependance technique. Pas de base de donnees, pas de framework web, pas d'import `requests` ou `sqlalchemy`. Juste des classes, des methodes et des regles metier.

!!! tip "L'avantage principal"
    Un modele de domaine pur se teste avec de simples tests unitaires, sans fixtures de base de donnees ni serveur HTTP. Les tests s'executent en millisecondes.

## Qu'est-ce qu'un Domain Model ?

Le Domain Model est une representation en code des concepts, des regles et des processus du domaine metier. C'est la traduction directe de ce que les experts metier decrivent quand ils parlent de leur travail.

Dans notre cas, les experts metier parlent de **lignes de commande**, de **lots de stock**, de **SKU** (Stock Keeping Unit), d'**allocation** et de **quantite disponible**. Le Domain Model reprend exactement ce vocabulaire.

```
Vocabulaire metier          Code
-----------------          ----
Ligne de commande    -->   OrderLine
Lot de stock         -->   Batch
Allouer              -->   allocate()
Quantite disponible  -->   available_quantity
Reference produit    -->   SKU (str)
```

La distinction fondamentale avec un transaction script, c'est l'endroit ou vivent les regles. Dans un transaction script, la logique est dans le handler :

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

Dans un Domain Model, la logique vit dans les objets du domaine eux-memes. Le handler ne fait que les orchestrer. C'est cette separation qui rend le code testable, lisible et maintenable.

## Value Objects

Un **Value Object** est un objet defini par ses attributs, pas par une identite. Deux billets de 10 euros sont interchangeables : peu importe lequel vous avez, ce qui compte c'est la valeur. De la meme facon, deux lignes de commande avec le meme `orderid`, le meme `sku` et la meme `qty` sont identiques.

Voici notre Value Object `OrderLine` :

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class OrderLine:
    """
    Value Object representant une ligne de commande.

    Un value object est immuable et defini par ses attributs,
    pas par une identite. Deux OrderLine avec les memes attributs
    sont considerees comme identiques.
    """

    orderid: str
    sku: str
    qty: int
```

Le decorateur `@dataclass(frozen=True)` fait deux choses essentielles :

1. **Immutabilite** -- On ne peut pas modifier les attributs apres creation. Un `line.qty = 5` levera une `FrozenInstanceError`. C'est voulu : un Value Object ne change pas, on en cree un nouveau si besoin.

2. **Hashabilite** -- Un objet `frozen` est automatiquement hashable, ce qui permet de l'utiliser dans des `set` et comme cle de `dict`. C'est indispensable pour notre modele, car `Batch` stocke ses allocations dans un `set[OrderLine]`.

??? note "Pourquoi `@dataclass` et pas `NamedTuple` ?"
    Les deux sont des choix valables. `@dataclass(frozen=True)` offre un peu plus de flexibilite (heritage, methodes, valeurs par defaut mutables via `field`). `NamedTuple` est legerement plus performant en memoire. Pour un Domain Model, la difference est negligeable. L'important, c'est l'immutabilite et l'egalite structurelle.

On peut verifier le comportement d'egalite :

```python
def test_equality():
    """Deux OrderLine avec les memes attributs sont egales (value object)."""
    line1 = OrderLine("order1", "SKU-001", 10)
    line2 = OrderLine("order1", "SKU-001", 10)
    assert line1 == line2

def test_inequality():
    line1 = OrderLine("order1", "SKU-001", 10)
    line2 = OrderLine("order2", "SKU-001", 10)
    assert line1 != line2
```

Pas besoin d'ecrire `__eq__` : `@dataclass` le genere automatiquement en comparant tous les attributs.

## Entities

Une **Entity** est un objet avec une identite qui persiste dans le temps. Meme si ses attributs changent, l'entite reste la meme. Un lot de stock avec la reference `"batch-042"` reste le meme lot, qu'il contienne 100 ou 50 unites.

Voici notre entite `Batch` :

```python
class Batch:
    """
    Entite representant un lot de stock.

    Un Batch a une identite (sa reference) et un cycle de vie.
    Il contient une quantite de stock pour un SKU donne,
    avec une date d'arrivee (ETA) optionnelle.
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

**`__eq__` compare uniquement la reference.** Deux objets `Batch` avec la meme reference sont consideres comme la meme entite, peu importe les autres attributs. C'est la definition meme d'une entite : l'egalite est basee sur l'identite, pas sur la valeur.

**`__hash__` est base sur la reference.** Quand on redefinit `__eq__`, Python rend l'objet non-hashable par defaut. On doit donc redefinir `__hash__` explicitement, en se basant sur le meme attribut que `__eq__`.

**`NotImplemented` plutot que `False`.** Quand on compare un `Batch` avec un objet d'un autre type, on retourne `NotImplemented` pour laisser Python essayer l'autre operande. C'est une bonne pratique souvent oubliee.

!!! warning "Entity vs Value Object : la regle"
    Si deux objets avec les memes attributs sont interchangeables, c'est un Value Object. Si un objet a un cycle de vie et une identite qui persiste meme quand ses attributs changent, c'est une Entity.

## Les regles metier dans le domaine

C'est ici que le Domain Model prend tout son sens. Les regles metier ne sont pas dans un service externe ou dans un handler : elles vivent directement dans les objets du domaine.

### Verifier qu'on peut allouer

```python
def can_allocate(self, line: OrderLine) -> bool:
    """Verifie si ce lot peut accueillir la ligne de commande."""
    return self.sku == line.sku and self.available_quantity >= line.qty
```

Deux conditions, et elles se lisent comme du langage naturel : le SKU doit correspondre, et la quantite disponible doit etre suffisante.

### Allouer une ligne de commande

```python
def allocate(self, line: OrderLine) -> None:
    """Alloue une ligne de commande a ce lot."""
    if self.can_allocate(line):
        self._allocations.add(line)
```

L'allocation revient a ajouter la ligne de commande dans l'ensemble `_allocations`. Comme `OrderLine` est un Value Object hashable, le `set` garantit l'idempotence : allouer deux fois la meme ligne n'a aucun effet.

### Desallouer

```python
def deallocate(self, line: OrderLine) -> None:
    """Desalloue une ligne de commande de ce lot."""
    if line in self._allocations:
        self._allocations.discard(line)
```

La desallocation est l'operation inverse. On utilise `discard` plutot que `remove` pour eviter une exception si la ligne n'est pas presente, mais la verification `if line in self._allocations` rend l'intention explicite.

### Quantites calculees

```python
@property
def allocated_quantity(self) -> int:
    return sum(line.qty for line in self._allocations)

@property
def available_quantity(self) -> int:
    return self._purchased_quantity - self.allocated_quantity
```

La quantite disponible est toujours calculee a partir de l'etat reel des allocations. Pas de compteur a maintenir manuellement, pas de risque de desynchronisation. C'est un choix de conception delibere : on prefere recalculer plutot que de maintenir un etat derive.

??? note "Performance"
    Recalculer `available_quantity` a chaque acces peut sembler couteux. En pratique, un lot a rarement plus de quelques dizaines d'allocations. Si la performance devenait un probleme, on pourrait ajouter un cache -- mais pas avant d'avoir mesure. L'optimisation prematuree est l'ennemi du code clair.

## La strategie d'allocation

Quand un client passe une commande, on veut allouer depuis le lot le plus pertinent. La regle metier est :

1. **D'abord les lots en stock** (ceux qui sont deja en entrepot, sans ETA).
2. **Puis les livraisons par ETA croissante** (la plus proche d'abord).

Pour implementer cette strategie, on definit `__gt__` sur `Batch` :

```python
def __gt__(self, other: Batch) -> bool:
    if self.eta is None:
        return False
    if other.eta is None:
        return True
    return self.eta > other.eta
```

La logique est la suivante :

- Un lot **sans ETA** (en stock) n'est jamais "plus grand" qu'un autre. Il sera donc toujours trie en premier.
- Un lot **avec ETA** est toujours "plus grand" qu'un lot sans ETA.
- Entre deux lots avec ETA, le tri se fait par date.

Cela permet d'utiliser simplement `sorted()` pour obtenir les lots dans l'ordre de preference :

```python
class Product:
    def allocate(self, line: OrderLine) -> str:
        """
        Alloue une ligne de commande au lot le plus approprie.

        La strategie d'allocation privilegie les lots en stock
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

`sorted(self.batches)` trie les lots grace a `__gt__`. Puis on prend le premier qui peut accueillir la ligne (`can_allocate`). Si aucun lot ne convient, on emet un evenement `OutOfStock`.

!!! tip "Pourquoi `__gt__` et pas `__lt__` ?"
    Python a besoin d'un seul operateur de comparaison pour que `sorted()` fonctionne. On aurait pu definir `__lt__` a la place, avec la logique inversee. Le choix de `__gt__` est une convention : on considere que les lots les "plus grands" sont ceux qui arrivent le plus tard, ce qui est naturel quand on pense aux dates.

## Tester le modele de domaine

L'avantage majeur d'un Domain Model pur, c'est la testabilite. Les tests sont simples, rapides et ne necessitent aucune infrastructure.

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

Remarquez la structure : chaque test cree ses objets, execute une action et verifie le resultat. Pas de `setUp` complexe, pas de mock, pas de base de donnees. Les noms des tests decrivent le comportement attendu en langage naturel.

### Tests de la strategie d'allocation

```python
class TestProduct:
    def test_prefers_warehouse_batches_to_shipments(self):
        """Les lots en stock (sans ETA) sont preferes aux livraisons."""
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
        """Parmi les livraisons, on prefere la plus proche."""
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

Le test `test_prefers_warehouse_batches_to_shipments` passe les lots dans l'ordre inverse (le lot en livraison avant celui en stock) pour verifier que le tri fonctionne. Le test `test_prefers_earlier_batches` melange volontairement l'ordre (`medium, earliest, latest`) pour la meme raison.

Ces tests s'executent en quelques millisecondes. On peut en avoir des centaines sans que la suite de tests ne ralentisse. C'est un avantage considerable par rapport aux tests d'integration qui necessitent une base de donnees.

## Resume

### Les concepts cles

| Concept | Description | Exemple |
|---------|-------------|---------|
| **Domain Model** | Couche de code pur qui represente les regles metier, sans dependance technique. | Le module `model.py` |
| **Value Object** | Objet defini par ses attributs, immuable, sans identite propre. | `OrderLine` |
| **Entity** | Objet avec une identite persistante, meme si ses attributs changent. | `Batch` |
| **Aggregate** | Entite racine qui garantit la coherence d'un groupe d'objets. | `Product` |

### Avantages du pattern

- **Testabilite** -- La logique metier se teste en isolation, sans infrastructure. Les tests sont rapides et fiables.
- **Lisibilite** -- Le code du domaine utilise le vocabulaire metier. Un expert non-technique peut le relire et verifier les regles.
- **Maintenabilite** -- Les regles metier sont centralisees. Quand une regle change, on sait exactement ou intervenir.
- **Independance technique** -- Le domaine ne depend pas de la base de donnees ni du framework web. On peut changer d'ORM ou de framework sans toucher aux regles metier.

### Inconvenients du pattern

- **Complexite initiale** -- Pour des CRUD simples, un Domain Model est excessif. Un transaction script suffit.
- **Mapping objet-relationnel** -- Le domaine etant decouple de la persistance, il faut une couche de mapping (c'est le sujet du prochain chapitre sur le Repository pattern).
- **Courbe d'apprentissage** -- Les concepts de DDD (Entity, Value Object, Aggregate) demandent un investissement initial.

!!! tip "Quand utiliser ce pattern ?"
    Le Domain Model vaut l'investissement quand la logique metier est complexe et susceptible d'evoluer. Si votre application est essentiellement un CRUD avec peu de regles metier, un transaction script ou un framework comme Django avec ses modeles "fat" sera plus adapte. Il n'y a pas de honte a choisir la simplicite quand elle suffit.

---

*Dans le prochain chapitre, nous verrons comment persister ce modele de domaine sans le contaminer avec des details techniques, grace au pattern **Repository**.*
