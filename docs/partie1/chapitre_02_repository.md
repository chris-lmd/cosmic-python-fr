# Chapitre 2 -- Le pattern Repository

## Le problème de la persistance

Au chapitre précédent, nous avons construit un modèle de domaine riche : des `OrderLine`, des `Batch`, un agrégat `Product` avec des règles métier claires. Tout fonctionne en mémoire, les tests passent, la logique est pure.

Mais une application réelle doit **sauvegarder ses données**. Les objets du domaine doivent être persistés dans une base de données, puis rechargées plus tard. Et c'est là que les ennuis commencent.

La tentation naturelle est d'ajouter des méthodes `save()` et `load()` directement dans le modèle de domaine :

```python
# Ce qu'on veut éviter
class Product:
    def save(self):
        db.execute("INSERT INTO products ...")

    @classmethod
    def load(cls, sku):
        row = db.execute("SELECT * FROM products WHERE sku = ?", sku)
        return cls(**row)
```

Ce code mélange deux responsabilités : la logique métier et l'accès aux données. Le modèle de domaine, qui était pur et testable, devient soudain dépendant de la base de données.

!!! danger "Le piège"
    Si le modèle de domaine connaît la BDD, chaque test unitaire devra instancier une connexion. Les tests deviennent lents, fragiles, et difficiles à maintenir.

La question est donc : **comment persister les objets du domaine sans polluer le modèle ?**

La réponse : le pattern Repository.


## Le pattern Repository

Le Repository est une abstraction qui donne **l'illusion d'une collection d'objets en mémoire**. Du point de vue du code qui l'utilise, un repository ressemble à un simple `set` ou une `list` Python : on peut y ajouter des objets, en récupérer, sans jamais se soucier de la façon dont ils sont stockés.

L'interface est volontairement minimale :

- **`add(product)`** -- ajouter un nouvel agrégat
- **`get(sku)`** -- récupérer un agrégat existant par son identifiant

C'est tout. Pas de `save()`, pas de `update()`, pas de `delete()`. Le repository cache toute la complexité de la persistance derrière cette interface élémentaire.

```
Code métier                    Repository                     BDD
-----------                    ----------                     ---
                  add(product)                  INSERT INTO ...
product = repo ───────────────> repo ─────────────────────────> DB
                  get(sku)                     SELECT * FROM ...
product = repo <─────────────── repo <───────────────────────── DB
```

Le domaine ne sait pas **comment** les objets sont stockés. PostgreSQL ? SQLite ? Un fichier JSON ? Un service distant ? Peu importe. Le contrat est le même.


## Le port : l'interface abstraite

Dans notre projet, le port est défini par la classe `AbstractRepository`. C'est une classe abstraite qui établit le contrat que toute implémentation doit respecter.

Voici le code de `src/allocation/adapters/repository.py` :

```python
import abc
from allocation.domain import model


class AbstractRepository(abc.ABC):
    """
    Interface abstraite du repository.

    Définit le contrat que tout repository doit respecter.
    Le pattern repose sur deux opérations fondamentales :
    - add : ajouter un nouvel agrégat
    - get : récupérer un agrégat existant
    """

    seen: set[model.Product]

    def __init__(self) -> None:
        self.seen: set[model.Product] = set()

    def add(self, product: model.Product) -> None:
        """Ajoute un produit au repository et le marque comme vu."""
        self._add(product)
        self.seen.add(product)

    def get(self, sku: str) -> model.Product | None:
        """Récupère un produit par son SKU et le marque comme vu."""
        product = self._get(sku)
        if product:
            self.seen.add(product)
        return product

    def get_by_batchref(self, batchref: str) -> model.Product | None:
        """Récupère un produit contenant le batch de référence donnée."""
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

### Méthodes publiques et méthodes abstraites protégées

Les méthodes publiques (`add`, `get`, `get_by_batchref`) ne sont **pas** abstraites. Elles contiennent la logique commune à toutes les implémentations -- en l'occurrence, le suivi des objets dans `self.seen`. Les méthodes abstraites préfixées d'un underscore (`_add`, `_get`, `_get_by_batchref`) sont les points d'extension que chaque implémentation concrète doit fournir.

Ce pattern (parfois appelé **Template Method**) garantit que le comportement de suivi est appliqué uniformément, quelle que soit l'implémentation.

### L'attribut `seen`

L'ensemble `seen` trace tous les objets qui ont été ajoutés ou consultés via le repository. Cet attribut est crucial pour le pattern Unit of Work (que nous verrons au chapitre 6) : il permet de savoir quels agrégats ont été manipulés au cours d'une transaction, et donc quels events doivent être collectés et traités.

```python
repo.add(product)          # product est ajouté à seen
product = repo.get("SKU")  # product est ajouté à seen
# -> self.seen contient tous les agrégats touchés
```

### Le vocabulaire Ports and Adapters

Dans l'architecture **Ports and Adapters** (aussi appelée architecture hexagonale), un **port** est une interface que le domaine définit pour communiquer avec le monde extérieur. `AbstractRepository` est un port : il exprime ce que le domaine **attend** de la couche de persistance, sans dicter comment l'implémenter.

!!! info "Port = interface définie par le domaine"
    Le port appartient au domaine. C'est le domaine qui dicte le contrat : "Je veux pouvoir ajouter un `Product` et en récupérer un par son SKU." La couche infrastructure doit s'y conformer.


## L'adapter concret : SQLAlchemy

Un **adapter** est une implémentation concrète d'un port. Il fait le lien entre l'abstraction définie par le domaine et une technologie spécifique. Dans notre cas, `SqlAlchemyRepository` est l'adapter qui connecte le port `AbstractRepository` à une base de données via SQLAlchemy.

```python
from sqlalchemy.orm import Session
from allocation.domain import model


class SqlAlchemyRepository(AbstractRepository):
    """
    Implémentation concrète du repository avec SQLAlchemy.

    Utilise une session SQLAlchemy pour persister et récupérer
    les agrégats Product.
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

1. **L'appel à `super().__init__()`** initialise le `set` `seen` dans la classe parente.
2. **`_add`** délègue simplement à `session.add()` de SQLAlchemy. La session se charge du tracking et de l'insertion.
3. **`_get`** utilise l'API de requêtage de SQLAlchemy pour filtrer par SKU.
4. **`_get_by_batchref`** fait une jointure pour trouver le `Product` à partir d'une référence de batch.

!!! note "Adapter = implémentation concrète du port"
    L'adapter traduit les opérations abstraites du port en appels concrets à une technologie. Si demain on migre vers MongoDB, on écrit un `MongoRepository` qui implémente les mêmes méthodes `_add`, `_get`, `_get_by_batchref`. Le reste du code ne change pas.


## Persistence Ignorance

Un principe fondamental de cette architecture est la **Persistence Ignorance** : le modèle de domaine ne sait absolument rien de la base de données. Il n'importe pas SQLAlchemy, ne connaît pas les tables, n'a pas de méthodes `save()`.

Regardez la classe `Product` dans `src/allocation/domain/model.py` :

```python
class Product:
    """
    Agrégat racine pour la gestion des produits.
    """

    def __init__(self, sku: str, batches: list[Batch] | None = None,
                 version_number: int = 0):
        self.sku = sku
        self.batches = batches or []
        self.version_number = version_number
        self.events: list[events.Event] = []

    def allocate(self, line: OrderLine) -> str:
        # ... logique métier pure ...
```

Aucune référence à la BDD. Aucun import de SQLAlchemy. La classe `Product` est un objet Python ordinaire, testable en isolation totale.

### Comment ça marche alors ?

C'est le module `src/allocation/adapters/orm.py` qui fait le lien, en utilisant le **classical mapping** de SQLAlchemy. Ce mécanisme permet de définir les tables d'un côté, les classes du domaine de l'autre, et de les associer explicitement :

```python
from sqlalchemy import Column, Date, ForeignKey, Integer, MetaData, String, Table
from sqlalchemy.orm import registry, relationship
from allocation.domain import model

metadata = MetaData()
mapper_registry = registry(metadata=metadata)

# Définition des tables
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
    L'approche classique de SQLAlchemy (utilisée ici via `map_imperatively`) est plus verbeuse que l'approche déclarative (où les classes héritent de `Base`), mais elle a un avantage crucial : **le modèle de domaine reste totalement indépendant de l'ORM**. Les classes `Product`, `Batch` et `OrderLine` n'héritent d'aucune classe SQLAlchemy.

La fonction `start_mappers()` est appelée une seule fois au démarrage de l'application. À partir de ce moment, SQLAlchemy sait comment convertir les objets du domaine en lignes de table, et inversement.


## Dependency Inversion

Le pattern Repository illustre parfaitement le **principe d'inversion des dépendances** (le "D" de SOLID). Comparons deux approches :

### Approche classique (dépendance directe)

```
Domaine ──depends on──> Infrastructure (SQLAlchemy)
```

Le domaine importe et utilise directement SQLAlchemy. Il est couplé à une technologie spécifique.

### Notre approche (dépendance inversée)

```
Domaine ──définit──> AbstractRepository (port)
                           ^
                           |
                       implémente
                           |
Infrastructure ────> SqlAlchemyRepository (adapter)
```

Le domaine définit l'interface (`AbstractRepository`). L'infrastructure l'implémente (`SqlAlchemyRepository`). Les dépendances pointent **vers l'intérieur**, vers le domaine.

!!! success "Conséquence"
    Le domaine ne dépend de rien. C'est l'infrastructure qui dépend du domaine. Si on veut changer de base de données, on ne touche pas au domaine -- on écrit un nouvel adapter.

Ce principe se généralise à toute communication avec le monde extérieur : envoyer un email, appeler une API, lire un fichier. Le domaine définit le port (ce dont il a besoin), et l'infrastructure fournit l'adapter (comment le faire concrètement).


## Fake Repository pour les tests

L'un des bénéfices les plus immédiats du pattern Repository est la possibilité de créer un **fake** pour les tests. Puisque le contrat est défini par l'interface abstraite, on peut écrire une implémentation qui stocke tout en mémoire, dans un simple `set` Python.

Voici le `FakeRepository` utilisé dans `tests/unit/test_handlers.py` :

```python
class FakeRepository(AbstractRepository):
    """
    Fake repository qui stocke les produits en mémoire.
    Utilisé pour les tests unitaires.
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

C'est tout. Pas de base de données, pas de fichier de configuration, pas de conteneur Docker. Juste un `set` Python.

### Pourquoi c'est puissant

Les tests qui utilisent le `FakeRepository` sont :

- **Rapides** -- pas de connexion à une BDD, pas d'I/O. Les tests s'exécutent en millisecondes.
- **Isolés** -- chaque test crée son propre fake, sans effet de bord.
- **Déterministes** -- pas de problème d'état partagé, de données résiduelles ou de transactions concurrentes.
- **Faciles à écrire** -- pas besoin de fixtures complexes pour initialiser la base.

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

Le `FakeRepository` est imbriqué dans un `FakeUnitOfWork` (que nous détaillerons au chapitre 6), mais le principe est le même : on remplace l'adapter concret par un fake, et le code métier ne voit pas la différence.

!!! info "Fake vs Mock"
    Un **fake** est une implémentation simplifiée mais fonctionnelle d'une interface. Il a un vrai comportement (ici : stocker et retrouver des objets). Un **mock**, en revanche, se contente de vérifier que certaines méthodes ont été appelées avec certains arguments. Les fakes sont généralement préférables car ils testent le **comportement** plutôt que l'**implémentation**.


## Le schéma d'ensemble

Récapitulons comment les pièces s'assemblent :

```
src/allocation/
    domain/
        model.py              <-- Modèle de domaine (Product, Batch, OrderLine)
                                   Ne connaît PAS la BDD
    adapters/
        repository.py         <-- AbstractRepository (port)
                                   + SqlAlchemyRepository (adapter)
        orm.py                <-- Classical mapping SQLAlchemy
                                   Fait le lien entre model.py et la BDD

tests/unit/
    test_handlers.py          <-- FakeRepository (fake adapter pour les tests)
```

Le flux est toujours le même :

1. Le code métier manipule un `AbstractRepository` (le port).
2. En production, c'est un `SqlAlchemyRepository` (l'adapter réel) qui est injecté.
3. En test, c'est un `FakeRepository` (le fake adapter) qui est injecté.
4. Le modèle de domaine reste ignorant de tout cela.


## Résumé

### Tableau des concepts

| Concept | Rôle | Fichier |
|---------|------|---------|
| **Repository** | Abstraction de la couche de persistance | `adapters/repository.py` |
| **Port** (`AbstractRepository`) | Interface définie par le domaine | `adapters/repository.py` |
| **Adapter** (`SqlAlchemyRepository`) | Implémentation concrète du port | `adapters/repository.py` |
| **Classical Mapping** | Liaison entre classes du domaine et tables SQL | `adapters/orm.py` |
| **Persistence Ignorance** | Le domaine ne connaît pas la BDD | `domain/model.py` |
| **Fake** (`FakeRepository`) | Implémentation en mémoire pour les tests | `tests/unit/test_handlers.py` |

### Avantages

- **Découplage** -- Le modèle de domaine ne dépend pas de la technologie de persistance. On peut changer de BDD sans modifier la logique métier.
- **Testabilité** -- Grâce au fake, les tests unitaires sont rapides, isolés et déterministes. Pas besoin de base de données pour tester la logique métier.
- **Lisibilité** -- L'interface `add()` / `get()` est simple et intuitive. Le code métier lit comme du langage naturel.
- **Extensibilité** -- Ajouter une nouvelle source de données revient à écrire un nouvel adapter. Le reste du système n'est pas affecté.

### Inconvénients

- **Complexité additionnelle** -- On introduit une couche d'abstraction supplémentaire (interface + implémentation + mapping ORM). Pour une application très simple, c'est du sur-engineering.
- **Courbe d'apprentissage** -- Le classical mapping de SQLAlchemy est moins intuitif que l'approche déclarative. Il faut comprendre le concept de ports and adapters pour saisir la motivation.
- **Code supplémentaire** -- Le fake doit être maintenu en parallèle de l'implémentation réelle. Si l'interface évolue, il faut mettre à jour les deux.

!!! quote "Règle d'or"
    Le pattern Repository n'est pas nécessaire pour toutes les applications. Il prend tout son sens quand la logique métier est suffisamment complexe pour mériter d'être isolée et testée indépendamment de la base de données.

---

**Prochain chapitre** : [Chapitre 3 -- Couplage et abstractions](chapitre_03_abstractions.md), où nous approfondirons le principe d'inversion des dépendances et les stratégies pour introduire des abstractions pertinentes.
