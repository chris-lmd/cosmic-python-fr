# Chapitre 13 -- Injection de dependances et bootstrap

## Le probleme : qui cree les dependances ?

Nos handlers ont besoin de collaborateurs pour fonctionner. Le handler `allocate`
a besoin d'un Unit of Work pour acceder aux produits et persister les changements.
Le handler `send_out_of_stock_notification` a besoin d'un adapter de notifications
pour envoyer un email. D'autres handlers pourraient avoir besoin d'un client Redis,
d'un logger specifique, ou de n'importe quel autre service d'infrastructure.

La question est simple en apparence, mais fondamentale : **qui cree ces objets,
et qui les passe aux handlers ?**

Examinons nos handlers tels qu'ils sont ecrits :

```python
# src/allocation/service_layer/handlers.py

def allocate(
    cmd: commands.Allocate,
    uow: AbstractUnitOfWork,        # <-- besoin d'un UoW
) -> str:
    line = model.OrderLine(orderid=cmd.orderid, sku=cmd.sku, qty=cmd.qty)
    with uow:
        product = uow.products.get(sku=cmd.sku)
        if product is None:
            raise InvalidSku(f"SKU inconnu : {cmd.sku}")
        batchref = product.allocate(line)
        uow.commit()
    return batchref


def send_out_of_stock_notification(
    event: events.OutOfStock,
    notifications: AbstractNotifications,  # <-- besoin de notifications
) -> None:
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )
```

Chaque handler **declare** ses dependances via ses parametres. Mais il ne les
cree pas lui-meme. C'est une decision deliberee : si `allocate` instanciait
directement un `SqlAlchemyUnitOfWork`, on ne pourrait plus le tester avec un
fake. Si `send_out_of_stock_notification` creait un `EmailNotifications`,
impossible de verifier les envois sans serveur SMTP.

On pourrait etre tente de resoudre cela de maniere ad hoc -- un import ici,
un singleton la -- mais cela mene rapidement a un reseau de dependances
implicites, difficile a suivre et encore plus difficile a tester.

---

## Dependency Injection (DI)

Le principe de la Dependency Injection est elegant dans sa simplicite :

!!! note "Dependency Injection"
    Au lieu qu'un composant **cree** ses dependances, elles lui sont
    **injectees de l'exterieur**. Le composant declare ce dont il a besoin
    (via ses parametres), et quelqu'un d'autre fournit les instances concretes.

C'est l'application directe du Dependency Inversion Principle vu au chapitre 3,
mais ici on passe a la mecanique : **comment** fournir les bonnes instances
aux bons handlers ?

Il y a trois approches classiques :

1. **Injection par constructeur** : les dependances sont passees a `__init__`.
   C'est le cas de notre `MessageBus`, qui recoit le `uow` et les `dependencies`
   a sa construction.

2. **Injection par parametre** : les dependances sont passees a chaque appel
   de fonction. C'est le cas de nos handlers : `allocate(cmd, uow=...)`.

3. **Injection par framework** : un conteneur DI resout automatiquement le
   graphe de dependances. On en parlera en fin de chapitre.

Notre architecture combine les deux premieres approches : le `MessageBus` recoit
ses dependances par constructeur, puis les **redistribue** aux handlers par
parametre a chaque appel.

---

## La Composition Root : un seul point d'assemblage

Dans toute application, il existe un moment ou il faut **assembler** les pieces :
creer les instances concretes et les connecter entre elles. Ce lieu s'appelle la
**Composition Root**.

Le principe est strict : **il ne doit y avoir qu'un seul endroit** dans
l'application ou les dependances concretes sont instanciees et reliees.
Partout ailleurs, le code ne manipule que des abstractions.

```
   Sans Composition Root :                Avec Composition Root :
   dependances creees partout             dependances creees en un seul point

   ┌───────────┐                          ┌───────────────────┐
   │ Handler A │── new UoW()              │   Bootstrap       │
   └───────────┘                          │   (Composition    │
   ┌───────────┐                          │    Root)          │
   │ Handler B │── new UoW()              │                   │
   └───────────┘                          │  uow = SqlUoW()  │
   ┌───────────┐                          │  notif = Email()  │
   │ Handler C │── new Email()            │  bus = MessageBus │
   └───────────┘                          │    (uow, notif)   │
                                          └─────────┬─────────┘
   Probleme : couplage direct,                      │
   impossible de remplacer                  ┌───────┴───────┐
   les implementations.                     │ Handlers A,B,C│
                                            │ recoivent les │
                                            │ dependances   │
                                            └───────────────┘
```

Dans notre projet, la Composition Root est le module **`bootstrap.py`**.

---

## Le module bootstrap.py

Voici le coeur de l'assemblage de notre application :

```python
# src/allocation/service_layer/bootstrap.py

def bootstrap(
    start_orm: bool = True,
    uow: unit_of_work.AbstractUnitOfWork | None = None,
    notifications_adapter: notifications.AbstractNotifications | None = None,
    **extra_dependencies: Any,
) -> messagebus.MessageBus:
    """
    Construit et retourne un MessageBus configure.
    """
    if start_orm:
        orm.start_mappers()

    if uow is None:
        uow = unit_of_work.SqlAlchemyUnitOfWork()

    if notifications_adapter is None:
        notifications_adapter = notifications.EmailNotifications()

    dependencies: dict[str, Any] = {
        "notifications": notifications_adapter,
        **extra_dependencies,
    }

    return messagebus.MessageBus(
        uow=uow,
        event_handlers=EVENT_HANDLERS,
        command_handlers=COMMAND_HANDLERS,
        dependencies=dependencies,
    )
```

Decomposons ce que fait cette fonction :

1. **Initialisation de l'ORM** : si `start_orm` est vrai, on demarre le mapping
   SQLAlchemy. En production, c'est necessaire. En tests unitaires, on passe
   `start_orm=False` car on n'a pas besoin de base de donnees.

2. **Creation du UoW** : si aucun `uow` n'est fourni, on cree le concret
   `SqlAlchemyUnitOfWork`. Si un fake est passe, on l'utilise tel quel.

3. **Creation des notifications** : meme logique. Par defaut, on cree
   `EmailNotifications`. En test, on passe un `FakeNotifications`.

4. **Assemblage du dictionnaire de dependances** : toutes les dependances
   supplementaires (notifications, et potentiellement d'autres) sont regroupees
   dans un dictionnaire.

5. **Construction du MessageBus** : le bus recoit le UoW, les mappings
   handlers/messages, et le dictionnaire de dependances.

Le second element cle est la **configuration des handlers**, definie dans le
meme module :

```python
# src/allocation/service_layer/bootstrap.py

EVENT_HANDLERS: dict[type[events.Event], list] = {
    events.Allocated: [handlers.publish_allocated_event],
    events.Deallocated: [handlers.reallocate],
    events.OutOfStock: [handlers.send_out_of_stock_notification],
}

COMMAND_HANDLERS: dict[type[commands.Command], Any] = {
    commands.CreateBatch: handlers.add_batch,
    commands.Allocate: handlers.allocate,
    commands.ChangeBatchQuantity: handlers.change_batch_quantity,
}
```

Ces dictionnaires constituent le **routing** de l'application : quel handler
traite quel message. Ils sont declares de maniere statique et centralisee,
ce qui rend le flux de l'application lisible d'un seul coup d'oeil.

### Utilisation en production

Dans le point d'entree Flask, le bootstrap est appele une seule fois au
demarrage de l'application :

```python
# src/allocation/entrypoints/flask_app.py

from allocation.service_layer import bootstrap

app = Flask(__name__)
bus = bootstrap.bootstrap()  # Composition Root -- tout est assemble ici
```

A partir de la, `bus` est le seul objet dont l'application a besoin. Chaque
endpoint se contente de creer une command et de la confier au bus :

```python
@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    cmd = commands.Allocate(
        orderid=data["orderid"], sku=data["sku"], qty=data["qty"],
    )
    results = bus.handle(cmd)
    batchref = results.pop(0)
    return jsonify({"batchref": batchref}), 201
```

L'endpoint ne sait pas quel UoW est utilise, ni comment les notifications sont
envoyees. Il n'a pas besoin de le savoir. Toute cette mecanique est cachee
derriere le `MessageBus`, assemble par le bootstrap.

---

## L'injection par introspection

Le mecanisme le plus subtil de notre architecture se trouve dans la methode
`_call_handler` du `MessageBus`. C'est elle qui realise l'injection de
dependances a chaque appel de handler.

```python
# src/allocation/service_layer/messagebus.py

def _call_handler(self, handler: Callable, message: Message) -> Any:
    """
    Appelle un handler en injectant les dependances necessaires.

    Introspection des parametres du handler pour determiner
    quelles dependances injecter.
    """
    import inspect

    params = inspect.signature(handler).parameters
    kwargs: dict[str, Any] = {}
    for name, param in params.items():
        if name == list(params.keys())[0]:
            # Premier parametre = le message lui-meme
            continue
        if name == "uow":
            kwargs[name] = self.uow
        elif name in self.dependencies:
            kwargs[name] = self.dependencies[name]

    return handler(message, **kwargs)
```

Voici ce qui se passe, etape par etape :

1. **Introspection** : `inspect.signature(handler).parameters` examine la
   signature du handler pour connaitre les noms de ses parametres.

2. **Le premier parametre est saute** : c'est toujours le message lui-meme
   (la command ou l'event). Il sera passe comme argument positionnel.

3. **Resolution des dependances** : pour chaque parametre restant, le bus
   cherche une correspondance par **nom** :
    - Si le parametre s'appelle `uow`, il recoit le `self.uow`.
    - Sinon, le bus cherche dans le dictionnaire `self.dependencies`.

4. **Appel du handler** : le message est passe en premier argument positionnel,
   les dependances en keyword arguments.

### Exemple concret

Prenons le handler `send_out_of_stock_notification` :

```python
def send_out_of_stock_notification(
    event: events.OutOfStock,
    notifications: AbstractNotifications,
) -> None:
    ...
```

Quand le bus doit appeler ce handler :

- `inspect.signature` detecte deux parametres : `event` et `notifications`.
- `event` est le premier parametre, il est saute.
- `notifications` est cherche dans `self.dependencies` -- et trouve, car
  le bootstrap a rempli `dependencies = {"notifications": notifications_adapter}`.
- Le bus appelle : `handler(event, notifications=email_adapter)`.

Pour le handler `allocate` :

```python
def allocate(
    cmd: commands.Allocate,
    uow: AbstractUnitOfWork,
) -> str:
    ...
```

- `cmd` est le premier parametre, saute.
- `uow` est detecte par son nom et recoit `self.uow`.
- Le bus appelle : `handler(cmd, uow=sqlalchemy_uow)`.

!!! tip "Convention over configuration"
    L'injection fonctionne par **convention de nommage** : un parametre
    nomme `uow` recoit le Unit of Work, un parametre nomme `notifications`
    recoit l'adapter de notifications. C'est simple, lisible, et ne
    necessite aucun decorateur ni annotation speciale.

---

## DI pour les tests

L'un des benefices majeurs de la Dependency Injection est de pouvoir
remplacer les implementations concretes par des **fakes** dans les tests.
Le meme code de handler est execute, mais avec des dependances differentes.

### Le bootstrap de test

Voici comment les tests unitaires assemblent le bus :

```python
# tests/unit/test_handlers.py

class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    """Fake Unit of Work utilisant le FakeRepository."""

    def __init__(self):
        self.products = FakeRepository([])
        self.committed = False

    def __enter__(self):
        return super().__enter__()

    def __exit__(self, *args):
        pass

    def _commit(self):
        self.committed = True

    def rollback(self):
        pass


class FakeNotifications(AbstractNotifications):
    """Fake pour capturer les notifications envoyees."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, destination: str, message: str) -> None:
        self.sent.append((destination, message))


def bootstrap_test_bus(uow: FakeUnitOfWork | None = None) -> messagebus.MessageBus:
    """Cree un message bus configure pour les tests."""
    from allocation.service_layer.bootstrap import EVENT_HANDLERS, COMMAND_HANDLERS

    uow = uow or FakeUnitOfWork()
    notifications = FakeNotifications()
    return messagebus.MessageBus(
        uow=uow,
        event_handlers=EVENT_HANDLERS,
        command_handlers=COMMAND_HANDLERS,
        dependencies={"notifications": notifications},
    )
```

Observez les points cles :

- **`FakeUnitOfWork`** remplace `SqlAlchemyUnitOfWork`. Les produits sont
  stockes en memoire dans un `FakeRepository`. Le `_commit` se contente de
  passer un flag a `True`, ce qui permet de verifier que le commit a eu lieu.

- **`FakeNotifications`** remplace `EmailNotifications`. Au lieu d'envoyer un
  email, chaque appel a `send` est enregistre dans une liste `self.sent`.
  Les tests peuvent ensuite inspecter cette liste.

- **`bootstrap_test_bus`** joue le role de Composition Root pour les tests.
  Elle reutilise les memes `EVENT_HANDLERS` et `COMMAND_HANDLERS` que la
  production (les handlers sont identiques), mais injecte des fakes.

### Les tests en action

Grace a cette architecture, les tests sont simples et expressifs :

```python
class TestAllocate:
    def test_allocate_returns_batch_ref(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "CHAISE-COMFY", 100, None))
        results = bus.handle(commands.Allocate("o1", "CHAISE-COMFY", 10))

        assert results.pop(0) == "b1"
```

Ce test traverse toute la pile applicative -- du `MessageBus` au handler,
du handler au domaine, du domaine au repository -- mais sans aucune
infrastructure reelle. Il s'execute en quelques millisecondes.

La cle : **le handler `allocate` ne sait pas qu'il travaille avec un fake**.
Il recoit un objet qui respecte le contrat `AbstractUnitOfWork`, et c'est
tout ce qui compte. C'est le polymorphisme au service de la testabilite.

### Le parallele production / tests

```
   Production :                         Tests :

   bootstrap()                          bootstrap_test_bus()
     │                                    │
     ├── SqlAlchemyUnitOfWork             ├── FakeUnitOfWork
     ├── EmailNotifications               ├── FakeNotifications
     └── MessageBus                       └── MessageBus
           │                                    │
           ├── handlers.allocate                ├── handlers.allocate
           ├── handlers.add_batch               ├── handlers.add_batch
           └── handlers.send_out_of_...         └── handlers.send_out_of_...

   Memes handlers, dependances differentes.
```

---

## Framework DI vs DI manuelle

En Java ou C#, la Dependency Injection passe presque toujours par un
**framework** : Spring, Guice, Autofac. Ces frameworks maintiennent un
**conteneur** qui connait toutes les classes de l'application, resout
automatiquement le graphe de dependances, et gere les cycles de vie
(singleton, scoped, transient).

En Python, la situation est differente. Le langage est suffisamment dynamique
pour que la DI manuelle soit souvent la meilleure option.

### Pourquoi la DI manuelle suffit en Python

Notre `bootstrap.py` fait une trentaine de lignes. Il est lisible, explicite,
et facile a debugger. Quand quelque chose ne va pas, on sait exactement ou
regarder : c'est dans le bootstrap.

Comparez avec un framework DI hypothetique :

```python
# Avec un framework DI (hypothetique)
container = Container()
container.register(AbstractUnitOfWork, SqlAlchemyUnitOfWork, scope="singleton")
container.register(AbstractNotifications, EmailNotifications, scope="transient")
container.register(MessageBus)
container.auto_wire()

bus = container.resolve(MessageBus)
```

C'est plus concis, mais aussi plus **magique**. Le `auto_wire()` cache la
mecanique de resolution. Quand ca ne fonctionne pas, le message d'erreur
peut etre cryptique. Et pour un projet de taille raisonnable, le gain
par rapport au bootstrap manuel est negligeable.

### Quand envisager un framework

Un framework DI devient interessant quand :

- Le nombre de dependances depasse la vingtaine et le bootstrap manuel
  devient penible a maintenir.
- Vous avez besoin de **scopes** complexes (par requete HTTP, par session,
  par thread).
- Plusieurs equipes travaillent sur le meme projet et ont besoin d'un
  mecanisme standardise pour enregistrer des composants.

Si vous atteignez ce stade, la bibliotheque
[dependency-injector](https://python-dependency-injector.ets-labs.org/)
est le choix le plus mature en Python. Elle offre des conteneurs, du wiring
automatique, et une bonne integration avec les frameworks web.

!!! warning "Ne commencez pas par un framework DI"
    La DI manuelle via un bootstrap est suffisante pour la grande majorite
    des projets Python. N'ajoutez un framework que quand la douleur du
    bootstrap manuel devient reelle, pas par anticipation.

---

## Resume

L'injection de dependances et le bootstrap resolvent un probleme fondamental
de toute architecture propre : **comment assembler les composants sans creer
de couplage entre eux**.

| Concept | Role | Dans notre code |
|---------|------|-----------------|
| **Dependency Injection** | Fournir les dependances de l'exterieur au lieu de les creer en interne | Les handlers declarent `uow`, `notifications` comme parametres |
| **Composition Root** | Un seul point ou les dependances concretes sont assemblees | `bootstrap.py` |
| **Bootstrap** | Fonction qui cree toutes les dependances et construit le bus | `bootstrap()` |
| **Introspection** | Decouvrir automatiquement les dependances requises par un handler | `inspect.signature` dans `_call_handler` |
| **Fakes pour les tests** | Implementations legeres pour tester sans infrastructure | `FakeUnitOfWork`, `FakeNotifications` |

### Architecture finale

Voici le schema complet de l'architecture, avec le bootstrap au sommet :

```
   ┌──────────────────────────────────────────────────────────────┐
   │                        BOOTSTRAP                            │
   │                   (Composition Root)                        │
   │                                                              │
   │  Cree :  UoW, Notifications, MessageBus                     │
   │  Configure : routing commands/events -> handlers             │
   └──────────────────────────┬───────────────────────────────────┘
                              │
                              │ construit
                              v
   ┌──────────────────────────────────────────────────────────────┐
   │                       MESSAGE BUS                            │
   │                                                              │
   │  - Recoit les commands et events                             │
   │  - Dispatch vers les handlers                                │
   │  - Injecte les dependances par introspection                 │
   │  - Collecte et traite les events en cascade                  │
   └──────┬──────────────────────────────────┬────────────────────┘
          │                                  │
          v                                  v
   ┌──────────────┐                   ┌──────────────┐
   │  Command      │                   │  Event        │
   │  Handlers     │                   │  Handlers     │
   │               │                   │               │
   │  allocate     │                   │  reallocate   │
   │  add_batch    │                   │  send_notif   │
   │  change_qty   │                   │  publish      │
   └──────┬───────┘                   └──────┬───────┘
          │                                  │
          │ utilisent                        │ utilisent
          v                                  v
   ┌──────────────────────────────────────────────────────────────┐
   │                      ABSTRACTIONS                            │
   │                                                              │
   │  AbstractUnitOfWork          AbstractNotifications            │
   │       │                            │                         │
   │       ├── SqlAlchemyUoW            ├── EmailNotifications    │
   │       └── FakeUoW (tests)          └── FakeNotifications     │
   └──────────────────────────────────────────────────────────────┘
          │                                  │
          v                                  v
   ┌──────────────┐                   ┌──────────────┐
   │  PostgreSQL   │                   │  Serveur     │
   │  (production) │                   │  SMTP        │
   └──────────────┘                   └──────────────┘
```

Le flux est toujours descendant : le bootstrap cree le bus, le bus dispatch
aux handlers, les handlers utilisent les abstractions, et les abstractions
cachent l'infrastructure. Nulle part un composant ne remonte pour creer
ou chercher ses propres dependances.

!!! tip "A retenir"
    - Un handler ne cree **jamais** ses dependances. Il les recoit.
    - Le **bootstrap** est le seul endroit ou les implementations concretes
      sont instanciees.
    - L'**introspection** (`inspect.signature`) permet au bus d'injecter
      automatiquement les bonnes dependances dans chaque handler.
    - En tests, on remplace les implementations concretes par des **fakes**
      via le meme mecanisme de bootstrap.
    - La DI manuelle via un bootstrap explicite est souvent **preferee** en
      Python a un framework DI. N'ajoutez de la complexite que quand elle
      est justifiee.
