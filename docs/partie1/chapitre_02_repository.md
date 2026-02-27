# Chapitre 2 -- Le pattern Repository

## Le probleme de la persistance

Au chapitre precedent, nous avons construit un modele de domaine riche : des `OrderLine`, des `Batch`, un agregat `Product` avec des regles metier claires. Tout fonctionne en memoire, les tests passent, la logique est pure.

Mais une application reelle doit **sauvegarder ses donnees**. Les objets du domaine doivent etre persistes dans une base de donnees, puis rechargees plus tard. Et c'est la que les ennuis commencent.

La tentation naturelle est d'ajouter des methodes `save()` et `load()` directement dans le modele de domaine :

```python
# Ce qu'on veut eviter
class Product:
    def save(self):
        db.execute("INSERT INTO products ...")

    @classmethod
    def load(cls, sku):
        row = db.execute("SELECT * FROM products WHERE sku = ?", sku)
        return cls(**row)
```

Ce code melange deux responsabilites : la logique metier et l'acces aux donnees. Le modele de domaine, qui etait pur et testable, devient soudain dependant de la base de donnees.

!!! danger "Le piege"
    Si le modele de domaine connait la BDD, chaque test unitaire devra instancier une connexion. Les tests deviennent lents, fragiles, et difficiles a maintenir.

La question est donc : **comment persister les objets du domaine sans polluer le modele ?**

La reponse : le pattern Repository.


## Le pattern Repository

Le Repository est une abstraction qui donne **l'illusion d'une collection d'objets en memoire**. Du point de vue du code qui l'utilise, un repository ressemble a un simple `set` ou une `list` Python : on peut y ajouter des objets, en recuperer, sans jamais se soucier de la facon dont ils sont stockes.

L'interface est volontairement minimale :

- **`add(product)`** -- ajouter un nouvel agregat
- **`get(sku)`** -- recuperer un agregat existant par son identifiant

C'est tout. Pas de `save()`, pas de `update()`, pas de `delete()`. Le repository cache toute la complexite de la persistance derriere cette interface elementaire.

```
Code metier                    Repository                     BDD
-----------                    ----------                     ---
                  add(product)                  INSERT INTO ...
product = repo ───────────────> repo ─────────────────────────> DB
                  get(sku)                     SELECT * FROM ...
product = repo <─────────────── repo <───────────────────────── DB
```

Le domaine ne sait pas **comment** les objets sont stockes. PostgreSQL ? SQLite ? Un fichier JSON ? Un service distant ? Peu importe. Le contrat est le meme.


## Le port : l'interface abstraite

Dans notre projet, le port est defini par la classe `AbstractRepository`. C'est une classe abstraite qui etablit le contrat que toute implementation doit respecter.

Voici le code de `src/allocation/adapters/repository.py` :

```python
import abc
from allocation.domain import model


class AbstractRepository(abc.ABC):
    """
    Interface abstraite du repository.

    Definit le contrat que tout repository doit respecter.
    Le pattern repose sur deux operations fondamentales :
    - add : ajouter un nouvel agregat
    - get : recuperer un agregat existant
    """

    seen: set[model.Product]

    def __init__(self) -> None:
        self.seen: set[model.Product] = set()

    def add(self, product: model.Product) -> None:
        """Ajoute un produit au repository et le marque comme vu."""
        self._add(product)
        self.seen.add(product)

    def get(self, sku: str) -> model.Product | None:
        """Recupere un produit par son SKU et le marque comme vu."""
        product = self._get(sku)
        if product:
            self.seen.add(product)
        return product

    def get_by_batchref(self, batchref: str) -> model.Product | None:
        """Recupere un produit contenant le batch de reference donnee."""
        product = self._get_by_batchref(batchref)
        if product:
            self.seen.add(product)
        return product

    @abc.abstractmethod
    def _add(self, product: model.Product) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def _get(self, sku: str) -> model.Product | None:
        raise NotImplementedError

    @abc.abstractmethod
    def _get_by_batchref(self, batchref: str) -> model.Product | None:
        raise NotImplementedError
```

Analysons les choix de conception :

### Methodes publiques et methodes abstraites protegees

Les methodes publiques (`add`, `get`, `get_by_batchref`) ne sont **pas** abstraites. Elles contiennent la logique commune a toutes les implementations -- en l'occurrence, le suivi des objets dans `self.seen`. Les methodes abstraites prefixees d'un underscore (`_add`, `_get`, `_get_by_batchref`) sont les points d'extension que chaque implementation concrete doit fournir.

Ce pattern (parfois appele **Template Method**) garantit que le comportement de suivi est applique uniformement, quelle que soit l'implementation.

### L'attribut `seen`

L'ensemble `seen` trace tous les objets qui ont ete ajoutes ou consultes via le repository. Cet attribut est crucial pour le pattern Unit of Work (que nous verrons au chapitre 6) : il permet de savoir quels agregats ont ete manipules au cours d'une transaction, et donc quels events doivent etre collectes et traites.

```python
repo.add(product)          # product est ajoute a seen
product = repo.get("SKU")  # product est ajoute a seen
# -> self.seen contient tous les agregats touches
```

### Le vocabulaire Ports and Adapters

Dans l'architecture **Ports and Adapters** (aussi appelee architecture hexagonale), un **port** est une interface que le domaine definit pour communiquer avec le monde exterieur. `AbstractRepository` est un port : il exprime ce que le domaine **attend** de la couche de persistance, sans dicter comment l'implementer.

!!! info "Port = interface definie par le domaine"
    Le port appartient au domaine. C'est le domaine qui dicte le contrat : "Je veux pouvoir ajouter un `Product` et en recuperer un par son SKU." La couche infrastructure doit s'y conformer.


## L'adapter concret : SQLAlchemy

Un **adapter** est une implementation concrete d'un port. Il fait le lien entre l'abstraction definie par le domaine et une technologie specifique. Dans notre cas, `SqlAlchemyRepository` est l'adapter qui connecte le port `AbstractRepository` a une base de donnees via SQLAlchemy.

```python
from sqlalchemy.orm import Session
from allocation.domain import model


class SqlAlchemyRepository(AbstractRepository):
    """
    Implementation concrete du repository avec SQLAlchemy.

    Utilise une session SQLAlchemy pour persister et recuperer
    les agregats Product.
    """

    def __init__(self, session: Session):
        super().__init__()
        self.session = session

    def _add(self, product: model.Product) -> None:
        self.session.add(product)

    def _get(self, sku: str) -> model.Product | None:
        return (
            self.session.query(model.Product)
            .filter_by(sku=sku)
            .first()
        )

    def _get_by_batchref(self, batchref: str) -> model.Product | None:
        return (
            self.session.query(model.Product)
            .join(model.Batch)
            .filter(model.Batch.reference == batchref)
            .first()
        )
```

Quelques observations :

1. **L'appel a `super().__init__()`** initialise le `set` `seen` dans la classe parente.
2. **`_add`** delegue simplement a `session.add()` de SQLAlchemy. La session se charge du tracking et de l'insertion.
3. **`_get`** utilise l'API de requetage de SQLAlchemy pour filtrer par SKU.
4. **`_get_by_batchref`** fait une jointure pour trouver le `Product` a partir d'une reference de batch.

!!! note "Adapter = implementation concrete du port"
    L'adapter traduit les operations abstraites du port en appels concrets a une technologie. Si demain on migre vers MongoDB, on ecrit un `MongoRepository` qui implemente les memes methodes `_add`, `_get`, `_get_by_batchref`. Le reste du code ne change pas.


## Persistence Ignorance

Un principe fondamental de cette architecture est la **Persistence Ignorance** : le modele de domaine ne sait absolument rien de la base de donnees. Il n'importe pas SQLAlchemy, ne connait pas les tables, n'a pas de methodes `save()`.

Regardez la classe `Product` dans `src/allocation/domain/model.py` :

```python
class Product:
    """
    Agregat racine pour la gestion des produits.
    """

    def __init__(self, sku: str, batches: list[Batch] | None = None,
                 version_number: int = 0):
        self.sku = sku
        self.batches = batches or []
        self.version_number = version_number
        self.events: list[events.Event] = []

    def allocate(self, line: OrderLine) -> str:
        # ... logique metier pure ...
```

Aucune reference a la BDD. Aucun import de SQLAlchemy. La classe `Product` est un objet Python ordinaire, testable en isolation totale.

### Comment ca marche alors ?

C'est le module `src/allocation/adapters/orm.py` qui fait le lien, en utilisant le **classical mapping** de SQLAlchemy. Ce mecanisme permet de definir les tables d'un cote, les classes du domaine de l'autre, et de les associer explicitement :

```python
from sqlalchemy import Column, Date, ForeignKey, Integer, MetaData, String, Table
from sqlalchemy.orm import registry, relationship
from allocation.domain import model

metadata = MetaData()
mapper_registry = registry(metadata=metadata)

# Definition des tables
order_lines = Table(
    "order_lines", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("orderid", String(255)),
    Column("sku", String(255)),
    Column("qty", Integer),
)

products = Table(
    "products", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sku", String(255)),
    Column("version_number", Integer, nullable=False, server_default="0"),
)

batches = Table(
    "batches", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reference", String(255)),
    Column("sku", String(255)),
    Column("_purchased_quantity", Integer),
    Column("eta", Date, nullable=True),
    Column("product_sku", String(255), ForeignKey("products.sku")),
)

allocations = Table(
    "allocations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("orderline_id", Integer, ForeignKey("order_lines.id")),
    Column("batch_id", Integer, ForeignKey("batches.id")),
)


def start_mappers() -> None:
    """
    Configure le mapping entre les classes du domaine et les tables SQL.
    """
    lines_mapper = mapper_registry.map_imperatively(
        model.OrderLine, order_lines
    )
    batches_mapper = mapper_registry.map_imperatively(
        model.Batch, batches,
        properties={
            "_allocations": relationship(
                lines_mapper, secondary=allocations, collection_class=set
            ),
        },
    )
    mapper_registry.map_imperatively(
        model.Product, products,
        properties={
            "batches": relationship(
                batches_mapper,
                primaryjoin=(products.c.sku == batches.c.product_sku),
            ),
        },
    )
```

!!! tip "Classical mapping vs. declarative"
    L'approche classique de SQLAlchemy (utilisee ici via `map_imperatively`) est plus verbeuse que l'approche declarative (ou les classes heritent de `Base`), mais elle a un avantage crucial : **le modele de domaine reste totalement independant de l'ORM**. Les classes `Product`, `Batch` et `OrderLine` n'heritent d'aucune classe SQLAlchemy.

La fonction `start_mappers()` est appelee une seule fois au demarrage de l'application. A partir de ce moment, SQLAlchemy sait comment convertir les objets du domaine en lignes de table, et inversement.


## Dependency Inversion

Le pattern Repository illustre parfaitement le **principe d'inversion des dependances** (le "D" de SOLID). Comparons deux approches :

### Approche classique (dependance directe)

```
Domaine ──depends on──> Infrastructure (SQLAlchemy)
```

Le domaine importe et utilise directement SQLAlchemy. Il est couple a une technologie specifique.

### Notre approche (dependance inversee)

```
Domaine ──definit──> AbstractRepository (port)
                           ^
                           |
                       implemente
                           |
Infrastructure ────> SqlAlchemyRepository (adapter)
```

Le domaine definit l'interface (`AbstractRepository`). L'infrastructure l'implemente (`SqlAlchemyRepository`). Les dependances pointent **vers l'interieur**, vers le domaine.

!!! success "Consequence"
    Le domaine ne depend de rien. C'est l'infrastructure qui depend du domaine. Si on veut changer de base de donnees, on ne touche pas au domaine -- on ecrit un nouvel adapter.

Ce principe se generalise a toute communication avec le monde exterieur : envoyer un email, appeler une API, lire un fichier. Le domaine definit le port (ce dont il a besoin), et l'infrastructure fournit l'adapter (comment le faire concretement).


## Fake Repository pour les tests

L'un des benefices les plus immediats du pattern Repository est la possibilite de creer un **fake** pour les tests. Puisque le contrat est defini par l'interface abstraite, on peut ecrire une implementation qui stocke tout en memoire, dans un simple `set` Python.

Voici le `FakeRepository` utilise dans `tests/unit/test_handlers.py` :

```python
class FakeRepository(AbstractRepository):
    """
    Fake repository qui stocke les produits en memoire.
    Utilise pour les tests unitaires.
    """

    def __init__(self, products: list[model.Product] | None = None):
        super().__init__()
        self._products = set(products or [])

    def _add(self, product: model.Product) -> None:
        self._products.add(product)

    def _get(self, sku: str) -> model.Product | None:
        return next((p for p in self._products if p.sku == sku), None)

    def _get_by_batchref(self, batchref: str) -> model.Product | None:
        return next(
            (
                p
                for p in self._products
                for b in p.batches
                if b.reference == batchref
            ),
            None,
        )
```

C'est tout. Pas de base de donnees, pas de fichier de configuration, pas de conteneur Docker. Juste un `set` Python.

### Pourquoi c'est puissant

Les tests qui utilisent le `FakeRepository` sont :

- **Rapides** -- pas de connexion a une BDD, pas d'I/O. Les tests s'executent en millisecondes.
- **Isoles** -- chaque test cree son propre fake, sans effet de bord.
- **Deterministes** -- pas de probleme d'etat partage, de donnees residuelles ou de transactions concurrentes.
- **Faciles a ecrire** -- pas besoin de fixtures complexes pour initialiser la base.

Voici un exemple de test concret utilisant le fake :

```python
class TestAddBatch:
    def test_add_batch_for_new_product(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "COUSSIN-CARRE", 100, None))

        assert bus.uow.products.get("COUSSIN-CARRE") is not None
        assert bus.uow.committed

    def test_add_batch_for_existing_product(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "LAMPE-RONDE", 100, None))
        bus.handle(commands.CreateBatch("b2", "LAMPE-RONDE", 99, None))

        product = bus.uow.products.get("LAMPE-RONDE")
        assert len(product.batches) == 2
```

Le `FakeRepository` est imbrique dans un `FakeUnitOfWork` (que nous detaillerons au chapitre 6), mais le principe est le meme : on remplace l'adapter concret par un fake, et le code metier ne voit pas la difference.

!!! info "Fake vs Mock"
    Un **fake** est une implementation simplifiee mais fonctionnelle d'une interface. Il a un vrai comportement (ici : stocker et retrouver des objets). Un **mock**, en revanche, se contente de verifier que certaines methodes ont ete appelees avec certains arguments. Les fakes sont generalement preferables car ils testent le **comportement** plutot que l'**implementation**.


## Le schema d'ensemble

Recapitulons comment les pieces s'assemblent :

```
src/allocation/
    domain/
        model.py              <-- Modele de domaine (Product, Batch, OrderLine)
                                   Ne connait PAS la BDD
    adapters/
        repository.py         <-- AbstractRepository (port)
                                   + SqlAlchemyRepository (adapter)
        orm.py                <-- Classical mapping SQLAlchemy
                                   Fait le lien entre model.py et la BDD

tests/unit/
    test_handlers.py          <-- FakeRepository (fake adapter pour les tests)
```

Le flux est toujours le meme :

1. Le code metier manipule un `AbstractRepository` (le port).
2. En production, c'est un `SqlAlchemyRepository` (l'adapter reel) qui est injecte.
3. En test, c'est un `FakeRepository` (le fake adapter) qui est injecte.
4. Le modele de domaine reste ignorant de tout cela.


## Resume

### Tableau des concepts

| Concept | Role | Fichier |
|---------|------|---------|
| **Repository** | Abstraction de la couche de persistance | `adapters/repository.py` |
| **Port** (`AbstractRepository`) | Interface definie par le domaine | `adapters/repository.py` |
| **Adapter** (`SqlAlchemyRepository`) | Implementation concrete du port | `adapters/repository.py` |
| **Classical Mapping** | Liaison entre classes du domaine et tables SQL | `adapters/orm.py` |
| **Persistence Ignorance** | Le domaine ne connait pas la BDD | `domain/model.py` |
| **Fake** (`FakeRepository`) | Implementation en memoire pour les tests | `tests/unit/test_handlers.py` |

### Avantages

- **Decouplage** -- Le modele de domaine ne depend pas de la technologie de persistance. On peut changer de BDD sans modifier la logique metier.
- **Testabilite** -- Grace au fake, les tests unitaires sont rapides, isoles et deterministes. Pas besoin de base de donnees pour tester la logique metier.
- **Lisibilite** -- L'interface `add()` / `get()` est simple et intuitive. Le code metier lit comme du langage naturel.
- **Extensibilite** -- Ajouter une nouvelle source de donnees revient a ecrire un nouvel adapter. Le reste du systeme n'est pas affecte.

### Inconvenients

- **Complexite additionnelle** -- On introduit une couche d'abstraction supplementaire (interface + implementation + mapping ORM). Pour une application tres simple, c'est du sur-engineering.
- **Courbe d'apprentissage** -- Le classical mapping de SQLAlchemy est moins intuitif que l'approche declarative. Il faut comprendre le concept de ports and adapters pour saisir la motivation.
- **Code supplementaire** -- Le fake doit etre maintenu en parallele de l'implementation reelle. Si l'interface evolue, il faut mettre a jour les deux.

!!! quote "Regle d'or"
    Le pattern Repository n'est pas necessaire pour toutes les applications. Il prend tout son sens quand la logique metier est suffisamment complexe pour meriter d'etre isolee et testee independamment de la base de donnees.

---

**Prochain chapitre** : [Chapitre 3 -- Couplage et abstractions](chapitre_03_abstractions.md), ou nous approfondirons le principe d'inversion des dependances et les strategies pour introduire des abstractions pertinentes.
