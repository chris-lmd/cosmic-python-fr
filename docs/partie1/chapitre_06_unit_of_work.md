# Chapitre 6 -- Le pattern Unit of Work

> **Comment garantir que les operations en base de donnees sont atomiques, sans coupler nos handlers a SQLAlchemy ?**

Jusqu'ici, notre architecture repose sur un repository qui abstrait l'acces a la base de donnees, et une service layer qui orchestre les cas d'usage. Mais une question reste ouverte : **qui gere la transaction ?**

Dans ce chapitre, nous introduisons le pattern **Unit of Work** -- un context manager qui encapsule la session, le repository et la transaction dans un seul objet coherent.

---

## Le probleme : qui controle la transaction ?

Notre handler `allocate` doit faire plusieurs choses dans une seule transaction :

1. Lire un `Product` depuis la base
2. Appeler la logique d'allocation sur le domaine
3. Persister le resultat en base
4. Commiter la transaction

La question est : **ou vit la gestion de la session et du commit ?**

### Option 1 : le repository gere la session

Si le repository cree et commite lui-meme sa session, chaque operation (`add`, `get`) est independante. On perd **l'atomicite** : si l'allocation reussit mais que le commit echoue, on se retrouve dans un etat incoherent.

```python
# Probleme : chaque appel est une transaction separee
product = repo.get("CHAISE-COMFY")       # transaction 1
batchref = product.allocate(line)         # en memoire
repo.save(product)                        # transaction 2 -- et si ca echoue ?
```

### Option 2 : le handler gere la session

Si le handler cree la session SQLAlchemy et la passe au repository, on retrouve l'atomicite. Mais le handler devient **couple a SQLAlchemy** -- exactement ce qu'on voulait eviter avec le repository.

```python
# Probleme : le handler connait SQLAlchemy
def allocate(cmd, session_factory):
    session = session_factory()
    repo = SqlAlchemyRepository(session)
    product = repo.get(cmd.sku)
    batchref = product.allocate(line)
    session.commit()  # le handler manipule directement la session
```

### La solution : un nouvel objet qui encapsule la transaction

Le **Unit of Work** resout ce dilemme. C'est un objet qui :

- **Cree la session** a l'entree du context manager
- **Fournit le repository** configure avec cette session
- **Expose `commit()` et `rollback()`** sans reveler l'implementation
- **Ferme la session** a la sortie, avec rollback automatique si `commit()` n'a pas ete appele

Le handler n'a plus besoin de connaitre SQLAlchemy. Il travaille avec une **abstraction**.

---

## Le pattern Unit of Work

Le Unit of Work represente une **unite de travail atomique**. C'est un concept formalise par Martin Fowler dans *Patterns of Enterprise Application Architecture* : un objet qui suit les modifications faites pendant une transaction et coordonne leur ecriture en base.

Dans notre implementation, le Unit of Work est un **context manager** Python. Voici comment un handler l'utilise :

```python
def allocate(cmd: Allocate, uow: AbstractUnitOfWork) -> str:
    line = OrderLine(orderid=cmd.orderid, sku=cmd.sku, qty=cmd.qty)
    with uow:
        product = uow.products.get(sku=cmd.sku)
        if product is None:
            raise InvalidSku(f"SKU inconnu : {cmd.sku}")
        batchref = product.allocate(line)
        uow.commit()
    return batchref
```

Les regles sont simples :

- `with uow:` ouvre la transaction et initialise le repository
- `uow.products` donne acces au repository (sans savoir comment il est construit)
- `uow.commit()` valide la transaction
- Si une exception survient avant le commit, `__exit__` declenche un **rollback automatique**
- La session est fermee dans tous les cas

---

## L'interface abstraite : `AbstractUnitOfWork`

L'interface est definie comme une classe abstraite qui implemente le **context manager protocol** de Python -- c'est-a-dire les methodes `__enter__` et `__exit__`.

```python title="src/allocation/service_layer/unit_of_work.py" hl_lines="5 8 11 14 19"
class AbstractUnitOfWork(abc.ABC):
    """
    Interface abstraite du Unit of Work.

    Definit le contrat : un repository products,
    et les methodes commit/rollback.
    """

    products: repository.AbstractRepository

    def __enter__(self) -> AbstractUnitOfWork:
        return self

    def __exit__(self, *args: object) -> None:
        self.rollback()

    def commit(self) -> None:
        self._commit()

    @abc.abstractmethod
    def _commit(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def rollback(self) -> None:
        raise NotImplementedError
```

### Anatomie du context manager protocol

Le protocol `with` de Python repose sur deux methodes speciales :

| Methode       | Quand ?                                 | Role dans le UoW                          |
|---------------|----------------------------------------|-------------------------------------------|
| `__enter__`   | A l'entree du bloc `with`              | Retourne `self` pour le `as`              |
| `__exit__`    | A la sortie du bloc `with` (toujours)  | Rollback automatique en cas d'erreur      |

Le point crucial est que `__exit__` est **toujours appele**, meme si une exception a lieu. C'est ce qui garantit le rollback automatique : si `commit()` n'a pas ete appele explicitement, `__exit__` appelle `rollback()`.

!!! note "Pourquoi `_commit` avec un underscore ?"
    La methode publique `commit()` est definie dans la classe abstraite. Elle delègue a `_commit()`, la methode abstraite que les sous-classes implementent. Ce decoupage permet d'ajouter de la logique commune dans `commit()` (par exemple collecter les events) sans que chaque implementation doive y penser.

---

## L'implementation SQLAlchemy

Voici l'implementation concrète qui utilise SQLAlchemy :

```python title="src/allocation/service_layer/unit_of_work.py"
DEFAULT_SESSION_FACTORY = sessionmaker(
    bind=create_engine(
        "sqlite:///allocation.db",
        isolation_level="SERIALIZABLE",
    )
)


class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    """
    Implementation concrète du UoW avec SQLAlchemy.

    Gère la session SQLAlchemy et le repository associe.
    """

    def __init__(self, session_factory: sessionmaker = DEFAULT_SESSION_FACTORY):
        self.session_factory = session_factory

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self.session: Session = self.session_factory()
        self.products = repository.SqlAlchemyRepository(self.session)
        return super().__enter__()

    def __exit__(self, *args: object) -> None:
        super().__exit__(*args)
        self.session.close()

    def _commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
```

### Le cycle de vie de la session

Voici ce qui se passe concretement lors de l'execution d'un handler :

```
with uow:                          # (1) __enter__ est appele
    |                              #     -> session = session_factory()
    |                              #     -> products = SqlAlchemyRepository(session)
    product = uow.products.get()   # (2) lecture via la session
    product.allocate(line)         # (3) logique metier pure
    uow.commit()                   # (4) session.commit()
                                   # (5) __exit__ est appele
                                   #     -> rollback() (sans effet apres commit)
                                   #     -> session.close()
```

Trois points importants :

1. **La session est creee a l'entree** (`__enter__`), pas dans le constructeur. Cela signifie qu'on peut reutiliser un UoW pour plusieurs transactions successives.

2. **Le rollback dans `__exit__` est un filet de securite.** Apres un `commit()` reussi, le `rollback()` n'a aucun effet. Mais si une exception survient avant le commit, il annule toutes les modifications.

3. **La session est toujours fermee** a la sortie, que la transaction ait reussi ou non. Pas de fuite de connexion.

!!! warning "Isolation level `SERIALIZABLE`"
    La session factory utilise le niveau d'isolation `SERIALIZABLE`, le plus strict. Cela garantit que deux transactions concurrentes ne peuvent pas modifier le meme produit simultanement. C'est essentiel pour l'allocation de stock ou les conditions de course (race conditions) pourraient mener a de la surallocation.

---

## La collecte des events : `collect_new_events`

Le Unit of Work joue un role supplementaire dans notre architecture : il sert de **pont entre les agrégats et le message bus**.

Quand un agrégat effectue une operation metier, il peut emettre des domain events. Par exemple, `Product.allocate()` emet un event `OutOfStock` si le stock est epuise, et `Product.change_batch_quantity()` emet des events `Deallocated` pour les lignes a reallouer.

Le probleme est : **comment le message bus recupere-t-il ces events ?**

C'est la methode `collect_new_events()` du UoW qui s'en charge :

```python title="src/allocation/service_layer/unit_of_work.py"
def collect_new_events(self):
    """
    Collecte tous les events emis par les agrégats vus
    au cours de cette transaction.
    """
    for product in self.products.seen:
        while product.events:
            yield product.events.pop(0)
```

### Comment ca fonctionne

Le mecanisme repose sur la collaboration entre le repository et le UoW :

1. Le repository garde une trace de tous les agrégats qu'il a **vus** (via `add` ou `get`), dans son attribut `seen`.
2. Chaque agrégat `Product` maintient une liste `events` ou il accumule ses domain events.
3. Apres chaque handler, le message bus appelle `uow.collect_new_events()`.
4. Cette methode itère sur les agrégats vus et **vide** leur liste d'events (avec `pop`).
5. Les events recuperes sont reinjectes dans la queue du message bus pour etre traites a leur tour.

```python title="src/allocation/service_layer/messagebus.py (extrait)"
def _handle_command(self, command: commands.Command) -> Any:
    handler = self.command_handlers.get(type(command))
    result = self._call_handler(handler, command)
    self.queue.extend(self.uow.collect_new_events())  # (1)
    return result
```

1. Apres chaque command, le bus collecte les events emis et les ajoute a sa queue.

C'est un mecanisme elegant : les agrégats n'ont pas besoin de connaitre le message bus, le bus n'a pas besoin de connaitre le domaine, et le UoW fait le lien entre les deux.

---

## Le Fake Unit of Work pour les tests

L'un des avantages majeurs du pattern est la **testabilite**. Puisque les handlers dependent de `AbstractUnitOfWork` (une abstraction), on peut facilement le remplacer par un fake dans les tests unitaires. Le `FakeUnitOfWork` utilise un `FakeRepository` qui stocke les produits en memoire (un simple `set`) :

```python title="tests/unit/test_handlers.py"
class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    """
    Fake Unit of Work utilisant le FakeRepository.
    Permet de tester sans base de donnees.
    """

    def __init__(self):
        self.products = FakeRepository([])
        self.committed = False  # (1)

    def __enter__(self):
        return super().__enter__()

    def __exit__(self, *args):
        pass

    def _commit(self):
        self.committed = True  # (2)

    def rollback(self):
        pass
```

1. L'attribut `committed` est initialise a `False`.
2. Quand `commit()` est appele (via `_commit`), il passe a `True`.

### L'attribut `committed` : verifier l'atomicite

L'attribut `committed` est un outil de test simple mais puissant. Il permet de **verifier que le handler a bien commite la transaction** :

```python
class TestAddBatch:
    def test_add_batch_for_new_product(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "COUSSIN-CARRE", 100, None))

        assert bus.uow.products.get("COUSSIN-CARRE") is not None
        assert bus.uow.committed  # on verifie que le commit a eu lieu
```

Sans cet attribut, on ne pourrait pas distinguer un handler qui modifie le repository sans commiter (ce qui serait un bug) d'un handler qui commite correctement.

### Le `FakeRepository` et l'attribut `seen`

Le `FakeRepository` herite de `AbstractRepository`, qui definit l'attribut `seen`. Cela signifie que `collect_new_events()` fonctionne **exactement de la meme maniere** avec le fake qu'avec l'implementation reelle. Les tests unitaires verifient donc le comportement complet, y compris la propagation des events.

!!! tip "Le pattern general des fakes"
    Un bon fake implemente la meme interface que le composant reel, avec un stockage en memoire. Il peut aussi exposer des attributs supplementaires (comme `committed`) pour les assertions. C'est plus fiable qu'un mock car on teste le **comportement** reel de l'interface, pas juste les appels de methodes.

---

## Le flux complet : du handler au domaine

Voici le flux complet quand le message bus traite une command `Allocate` :

```
MessageBus.handle(Allocate)
    |
    v
handler: allocate(cmd, uow)
    |
    +---> with uow:                          # UoW.__enter__
    |         |                               #   cree session + repository
    |         +---> uow.products.get(sku)     # Repository.get()
    |         |         |                     #   marque le Product comme "seen"
    |         |         v
    |         +---> product.allocate(line)     # Logique metier pure
    |         |         |                     #   peut emettre des events
    |         |         v
    |         +---> uow.commit()              # UoW.commit()
    |                   |                     #   session.commit()
    |                   v
    +---> (sortie du with)                    # UoW.__exit__
              |                               #   rollback() + session.close()
              v
MessageBus: uow.collect_new_events()          # Collecte les events
    |                                         #   emis par les agregats "seen"
    v
Traitement des events suivants...
```

Le diagramme en couches correspondant :

```
+------------------------------------------------------+
|                    Message Bus                        |
|   handle(command) -> handler -> collect_new_events()  |
+------------------------------------------------------+
          |                          ^
          v                          |
+------------------------------------------------------+
|                   Unit of Work                        |
|   __enter__  |  commit  |  rollback  |  __exit__     |
|   fournit: repository (products)                     |
+------------------------------------------------------+
          |                          ^
          v                          |
+------------------------------------------------------+
|                    Repository                         |
|   add(product)  |  get(sku)  |  seen: set[Product]   |
+------------------------------------------------------+
          |                          ^
          v                          |
+------------------------------------------------------+
|               Modele de Domaine                       |
|   Product -> Batch -> OrderLine                       |
|   events: [OutOfStock, Deallocated, ...]             |
+------------------------------------------------------+
```

---

## Resume

Le pattern **Unit of Work** resout le probleme de la gestion des transactions en introduisant un objet qui encapsule la session, le repository et la logique de commit/rollback.

### Ce que le Unit of Work apporte

| Aspect                | Sans UoW                              | Avec UoW                                |
|-----------------------|---------------------------------------|------------------------------------------|
| Transaction           | Geree par le handler ou le repository | Encapsulee dans le context manager       |
| Atomicite             | Difficile a garantir                  | Garantie par `__enter__`/`__exit__`      |
| Couplage              | Handler couple a SQLAlchemy           | Handler depend d'une abstraction         |
| Testabilite           | Necessite une base de donnees         | Fake UoW en memoire                      |
| Collecte des events   | Pas de mecanisme standard             | `collect_new_events()` centralise        |

### Les fichiers cles

| Fichier | Role |
|---------|------|
| `src/allocation/service_layer/unit_of_work.py` | Interface abstraite et implementation SQLAlchemy |
| `src/allocation/adapters/repository.py` | Repository avec tracking des agregats vus (`seen`) |
| `src/allocation/service_layer/handlers.py` | Handlers qui utilisent le UoW comme context manager |
| `tests/unit/test_handlers.py` | `FakeUnitOfWork` et `FakeRepository` pour les tests |

### Les principes a retenir

1. **Le UoW est un context manager** qui gere le cycle de vie de la transaction : ouverture, commit, rollback, fermeture.
2. **Le handler ne connait que l'abstraction** (`AbstractUnitOfWork`), jamais SQLAlchemy directement.
3. **Le rollback est automatique** : si `commit()` n'est pas appele explicitement, `__exit__` annule tout.
4. **Le UoW collecte les events** emis par les agregats au cours de la transaction, servant de pont vers le message bus.
5. **Le `FakeUnitOfWork` rend les tests rapides** et deterministes, sans base de donnees.

!!! abstract "Dans le prochain chapitre"
    Nous verrons le pattern **Aggregate** et la notion de **frontiere de coherence**. L'agregat `Product` definit le perimetre a l'interieur duquel les invariants metier sont garantis -- et le Unit of Work commite exactement un agregat par transaction.
