# Chapitre 13 -- Injection de dépendances et bootstrap

## Le problème : qui crée les dépendances ?

Nos handlers ont besoin de collaborateurs pour fonctionner. Le handler `allouer`
a besoin d'un Unit of Work pour accéder aux produits et persister les changements.
Le handler `envoyer_notification_rupture_stock` a besoin d'un adapter de notifications
pour envoyer un email. D'autres handlers pourraient avoir besoin d'un client Redis,
d'un logger spécifique, ou de n'importe quel autre service d'infrastructure.

La question est simple en apparence, mais fondamentale : **qui crée ces objets,
et qui les passe aux handlers ?**

Examinons nos handlers tels qu'ils sont écrits :

```python
# src/allocation/service_layer/handlers.py

def allouer(
    cmd: commands.Allouer,
    uow: AbstractUnitOfWork,        # <-- besoin d'un UoW
) -> str:
    ligne = model.LigneDeCommande(id_commande=cmd.id_commande, sku=cmd.sku, quantité=cmd.quantité)
    with uow:
        produit = uow.produits.get(sku=cmd.sku)
        if produit is None:
            raise SkuInconnu(f"SKU inconnu : {cmd.sku}")
        réf_lot = produit.allouer(ligne)
        uow.commit()
    return réf_lot


def envoyer_notification_rupture_stock(
    event: events.RuptureDeStock,
    notifications: AbstractNotifications,  # <-- besoin de notifications
) -> None:
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )
```

Chaque handler **déclare** ses dépendances via ses paramètres. Mais il ne les
crée pas lui-même. C'est une décision délibérée : si `allouer` instanciait
directement un `SqlAlchemyUnitOfWork`, on ne pourrait plus le tester avec un
fake. Si `envoyer_notification_rupture_stock` créait un `EmailNotifications`,
impossible de vérifier les envois sans serveur SMTP.

On pourrait être tenté de résoudre cela de manière ad hoc -- un import ici,
un singleton là -- mais cela mène rapidement à un réseau de dépendances
implicites, difficile à suivre et encore plus difficile à tester.

---

## Dependency Injection (DI)

Le principe de la Dependency Injection est élégant dans sa simplicité :

!!! note "Dependency Injection"
    Au lieu qu'un composant **crée** ses dépendances, elles lui sont
    **injectées de l'extérieur**. Le composant déclare ce dont il a besoin
    (via ses paramètres), et quelqu'un d'autre fournit les instances concrètes.

C'est l'application directe du Dependency Inversion Principle vu au chapitre 3,
mais ici on passe à la mécanique : **comment** fournir les bonnes instances
aux bons handlers ?

Il y a trois approches classiques :

1. **Injection par constructeur** : les dépendances sont passées à `__init__`.
   C'est le cas de notre `MessageBus`, qui reçoit le `uow` et les `dependencies`
   à sa construction.

2. **Injection par paramètre** : les dépendances sont passées à chaque appel
   de fonction. C'est le cas de nos handlers : `allouer(cmd, uow=...)`.

3. **Injection par framework** : un conteneur DI résout automatiquement le
   graphe de dépendances. On en parlera en fin de chapitre.

Notre architecture combine les deux premières approches : le `MessageBus` reçoit
ses dépendances par constructeur, puis les **redistribue** aux handlers par
paramètre à chaque appel.

---

## La Composition Root : un seul point d'assemblage

Dans toute application, il existe un moment où il faut **assembler** les pièces :
créer les instances concrètes et les connecter entre elles. Ce lieu s'appelle la
**Composition Root**.

Le principe est strict : **il ne doit y avoir qu'un seul endroit** dans
l'application où les dépendances concrètes sont instanciées et reliées.
Partout ailleurs, le code ne manipule que des abstractions.

```
   Sans Composition Root :                Avec Composition Root :
   dépendances créées partout             dépendances créées en un seul point

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
   Problème : couplage direct,                      │
   impossible de remplacer                  ┌───────┴───────┐
   les implémentations.                     │ Handlers A,B,C│
                                            │ reçoivent les │
                                            │ dépendances   │
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
    Construit et retourne un MessageBus configuré.
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

Décomposons ce que fait cette fonction :

1. **Initialisation de l'ORM** : si `start_orm` est vrai, on démarre le mapping
   SQLAlchemy. En production, c'est nécessaire. En tests unitaires, on passe
   `start_orm=False` car on n'a pas besoin de base de données.

2. **Création du UoW** : si aucun `uow` n'est fourni, on crée le concret
   `SqlAlchemyUnitOfWork`. Si un fake est passé, on l'utilise tel quel.

3. **Création des notifications** : même logique. Par défaut, on crée
   `EmailNotifications`. En test, on passe un `FakeNotifications`.

4. **Assemblage du dictionnaire de dépendances** : toutes les dépendances
   supplémentaires (notifications, et potentiellement d'autres) sont regroupées
   dans un dictionnaire.

5. **Construction du MessageBus** : le bus reçoit le UoW, les mappings
   handlers/messages, et le dictionnaire de dépendances.

Le second élément clé est la **configuration des handlers**, définie dans le
même module :

```python
# src/allocation/service_layer/bootstrap.py

EVENT_HANDLERS: dict[type[events.Event], list] = {
    events.Alloué: [handlers.publier_événement_allocation],
    events.Désalloué: [handlers.réallouer],
    events.RuptureDeStock: [handlers.envoyer_notification_rupture_stock],
}

COMMAND_HANDLERS: dict[type[commands.Command], Any] = {
    commands.CréerLot: handlers.ajouter_lot,
    commands.Allouer: handlers.allouer,
    commands.ModifierQuantitéLot: handlers.modifier_quantité_lot,
}
```

Ces dictionnaires constituent le **routing** de l'application : quel handler
traite quel message. Ils sont déclarés de manière statique et centralisée,
ce qui rend le flux de l'application lisible d'un seul coup d'oeil.

### Utilisation en production

Dans le point d'entrée Flask, le bootstrap est appelé une seule fois au
démarrage de l'application :

```python
# src/allocation/entrypoints/flask_app.py

from allocation.service_layer import bootstrap

app = Flask(__name__)
bus = bootstrap.bootstrap()  # Composition Root -- tout est assemblé ici
```

À partir de là, `bus` est le seul objet dont l'application a besoin. Chaque
endpoint se contente de créer une command et de la confier au bus :

```python
@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    cmd = commands.Allouer(
        id_commande=data["id_commande"], sku=data["sku"], quantité=data["quantité"],
    )
    results = bus.handle(cmd)
    réf_lot = results.pop(0)
    return jsonify({"réf_lot": réf_lot}), 201
```

L'endpoint ne sait pas quel UoW est utilisé, ni comment les notifications sont
envoyées. Il n'a pas besoin de le savoir. Toute cette mécanique est cachée
derrière le `MessageBus`, assemblé par le bootstrap.

---

## L'injection par introspection

Le mécanisme le plus subtil de notre architecture se trouve dans la méthode
`_call_handler` du `MessageBus`. C'est elle qui réalise l'injection de
dépendances à chaque appel de handler.

```python
# src/allocation/service_layer/messagebus.py

def _call_handler(self, handler: Callable, message: Message) -> Any:
    """
    Appelle un handler en injectant les dépendances nécessaires.

    Introspection des paramètres du handler pour déterminer
    quelles dépendances injecter.
    """
    import inspect

    params = inspect.signature(handler).parameters
    kwargs: dict[str, Any] = {}
    for name, param in params.items():
        if name == list(params.keys())[0]:
            # Premier paramètre = le message lui-même
            continue
        if name == "uow":
            kwargs[name] = self.uow
        elif name in self.dependencies:
            kwargs[name] = self.dependencies[name]

    return handler(message, **kwargs)
```

Voici ce qui se passe, étape par étape :

1. **Introspection** : `inspect.signature(handler).parameters` examine la
   signature du handler pour connaître les noms de ses paramètres.

2. **Le premier paramètre est sauté** : c'est toujours le message lui-même
   (la command ou l'event). Il sera passé comme argument positionnel.

3. **Résolution des dépendances** : pour chaque paramètre restant, le bus
   cherche une correspondance par **nom** :
    - Si le paramètre s'appelle `uow`, il reçoit le `self.uow`.
    - Sinon, le bus cherche dans le dictionnaire `self.dependencies`.

4. **Appel du handler** : le message est passé en premier argument positionnel,
   les dépendances en keyword arguments.

### Exemple concret

Prenons le handler `envoyer_notification_rupture_stock` :

```python
def envoyer_notification_rupture_stock(
    event: events.RuptureDeStock,
    notifications: AbstractNotifications,
) -> None:
    ...
```

Quand le bus doit appeler ce handler :

- `inspect.signature` détecte deux paramètres : `event` et `notifications`.
- `event` est le premier paramètre, il est sauté.
- `notifications` est cherché dans `self.dependencies` -- et trouvé, car
  le bootstrap a rempli `dependencies = {"notifications": notifications_adapter}`.
- Le bus appelle : `handler(event, notifications=email_adapter)`.

Pour le handler `allouer` :

```python
def allouer(
    cmd: commands.Allouer,
    uow: AbstractUnitOfWork,
) -> str:
    ...
```

- `cmd` est le premier paramètre, sauté.
- `uow` est détecté par son nom et reçoit `self.uow`.
- Le bus appelle : `handler(cmd, uow=sqlalchemy_uow)`.

!!! tip "Convention over configuration"
    L'injection fonctionne par **convention de nommage** : un paramètre
    nommé `uow` reçoit le Unit of Work, un paramètre nommé `notifications`
    reçoit l'adapter de notifications. C'est simple, lisible, et ne
    nécessite aucun décorateur ni annotation spéciale.

---

## DI pour les tests

L'un des bénéfices majeurs de la Dependency Injection est de pouvoir
remplacer les implémentations concrètes par des **fakes** dans les tests.
Le même code de handler est exécuté, mais avec des dépendances différentes.

### Le bootstrap de test

Voici comment les tests unitaires assemblent le bus :

```python
# tests/unit/test_handlers.py

class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    """Fake Unit of Work utilisant le FakeRepository."""

    def __init__(self):
        self.produits = FakeRepository([])
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
    """Fake pour capturer les notifications envoyées."""

    def __init__(self):
        self.envoyées: list[tuple[str, str]] = []

    def send(self, destination: str, message: str) -> None:
        self.envoyées.append((destination, message))


def bootstrap_test_bus(uow: FakeUnitOfWork | None = None) -> messagebus.MessageBus:
    """Crée un message bus configuré pour les tests."""
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

Observez les points clés :

- **`FakeUnitOfWork`** remplace `SqlAlchemyUnitOfWork`. Les produits sont
  stockés en mémoire dans un `FakeRepository`. Le `_commit` se contente de
  passer un flag à `True`, ce qui permet de vérifier que le commit a eu lieu.

- **`FakeNotifications`** remplace `EmailNotifications`. Au lieu d'envoyer un
  email, chaque appel à `send` est enregistré dans une liste `self.envoyées`.
  Les tests peuvent ensuite inspecter cette liste.

- **`bootstrap_test_bus`** joue le rôle de Composition Root pour les tests.
  Elle réutilise les mêmes `EVENT_HANDLERS` et `COMMAND_HANDLERS` que la
  production (les handlers sont identiques), mais injecte des fakes.

### Les tests en action

Grâce à cette architecture, les tests sont simples et expressifs :

```python
class TestAllouer:
    def test_allouer_retourne_réf_lot(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("b1", "CHAISE-COMFY", 100, None))
        results = bus.handle(commands.Allouer("o1", "CHAISE-COMFY", 10))

        assert results.pop(0) == "b1"
```

Ce test traverse toute la pile applicative -- du `MessageBus` au handler,
du handler au domaine, du domaine au repository -- mais sans aucune
infrastructure réelle. Il s'exécute en quelques millisecondes.

La clé : **le handler `allouer` ne sait pas qu'il travaille avec un fake**.
Il reçoit un objet qui respecte le contrat `AbstractUnitOfWork`, et c'est
tout ce qui compte. C'est le polymorphisme au service de la testabilité.

### Le parallèle production / tests

```
   Production :                         Tests :

   bootstrap()                          bootstrap_test_bus()
     │                                    │
     ├── SqlAlchemyUnitOfWork             ├── FakeUnitOfWork
     ├── EmailNotifications               ├── FakeNotifications
     └── MessageBus                       └── MessageBus
           │                                    │
           ├── handlers.allouer                 ├── handlers.allouer
           ├── handlers.ajouter_lot             ├── handlers.ajouter_lot
           └── handlers.envoyer_notif...        └── handlers.envoyer_notif...

   Mêmes handlers, dépendances différentes.
```

---

## Framework DI vs DI manuelle

En Java ou C#, la Dependency Injection passe presque toujours par un
**framework** : Spring, Guice, Autofac. Ces frameworks maintiennent un
**conteneur** qui connaît toutes les classes de l'application, résout
automatiquement le graphe de dépendances, et gère les cycles de vie
(singleton, scoped, transient).

En Python, la situation est différente. Le langage est suffisamment dynamique
pour que la DI manuelle soit souvent la meilleure option.

### Pourquoi la DI manuelle suffit en Python

Notre `bootstrap.py` fait une trentaine de lignes. Il est lisible, explicite,
et facile à débugger. Quand quelque chose ne va pas, on sait exactement où
regarder : c'est dans le bootstrap.

Comparez avec un framework DI hypothétique :

```python
# Avec un framework DI (hypothétique)
container = Container()
container.register(AbstractUnitOfWork, SqlAlchemyUnitOfWork, scope="singleton")
container.register(AbstractNotifications, EmailNotifications, scope="transient")
container.register(MessageBus)
container.auto_wire()

bus = container.resolve(MessageBus)
```

C'est plus concis, mais aussi plus **magique**. Le `auto_wire()` cache la
mécanique de résolution. Quand ça ne fonctionne pas, le message d'erreur
peut être cryptique. Et pour un projet de taille raisonnable, le gain
par rapport au bootstrap manuel est négligeable.

### Quand envisager un framework

Un framework DI devient intéressant quand :

- Le nombre de dépendances dépasse la vingtaine et le bootstrap manuel
  devient pénible à maintenir.
- Vous avez besoin de **scopes** complexes (par requête HTTP, par session,
  par thread).
- Plusieurs équipes travaillent sur le même projet et ont besoin d'un
  mécanisme standardisé pour enregistrer des composants.

Si vous atteignez ce stade, la bibliothèque
[dependency-injector](https://python-dependency-injector.ets-labs.org/)
est le choix le plus mature en Python. Elle offre des conteneurs, du wiring
automatique, et une bonne intégration avec les frameworks web.

!!! warning "Ne commencez pas par un framework DI"
    La DI manuelle via un bootstrap est suffisante pour la grande majorité
    des projets Python. N'ajoutez un framework que quand la douleur du
    bootstrap manuel devient réelle, pas par anticipation.

---

## Résumé

L'injection de dépendances et le bootstrap résolvent un problème fondamental
de toute architecture propre : **comment assembler les composants sans créer
de couplage entre eux**.

| Concept | Rôle | Dans notre code |
|---------|------|-----------------|
| **Dependency Injection** | Fournir les dépendances de l'extérieur au lieu de les créer en interne | Les handlers déclarent `uow`, `notifications` comme paramètres |
| **Composition Root** | Un seul point où les dépendances concrètes sont assemblées | `bootstrap.py` |
| **Bootstrap** | Fonction qui crée toutes les dépendances et construit le bus | `bootstrap()` |
| **Introspection** | Découvrir automatiquement les dépendances requises par un handler | `inspect.signature` dans `_call_handler` |
| **Fakes pour les tests** | Implémentations légères pour tester sans infrastructure | `FakeUnitOfWork`, `FakeNotifications` |

### Architecture finale

Voici le schéma complet de l'architecture, avec le bootstrap au sommet :

```
   ┌──────────────────────────────────────────────────────────────┐
   │                        BOOTSTRAP                            │
   │                   (Composition Root)                        │
   │                                                              │
   │  Crée :  UoW, Notifications, MessageBus                     │
   │  Configure : routing commands/events -> handlers             │
   └──────────────────────────┬───────────────────────────────────┘
                              │
                              │ construit
                              v
   ┌──────────────────────────────────────────────────────────────┐
   │                       MESSAGE BUS                            │
   │                                                              │
   │  - Reçoit les commands et events                             │
   │  - Dispatch vers les handlers                                │
   │  - Injecte les dépendances par introspection                 │
   │  - Collecte et traite les events en cascade                  │
   └──────┬──────────────────────────────────┬────────────────────┘
          │                                  │
          v                                  v
   ┌──────────────┐                   ┌──────────────┐
   │  Command      │                   │  Event        │
   │  Handlers     │                   │  Handlers     │
   │               │                   │               │
   │  allouer      │                   │  réallouer    │
   │  ajouter_lot  │                   │  envoyer_notif│
   │  modifier_qté │                   │  publier      │
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

Le flux est toujours descendant : le bootstrap crée le bus, le bus dispatch
aux handlers, les handlers utilisent les abstractions, et les abstractions
cachent l'infrastructure. Nulle part un composant ne remonte pour créer
ou chercher ses propres dépendances.

## Exercices

!!! example "Exercice 1 -- Nouvelle dépendance"
    Ajoutez un `AbstractLogger` injectable dans les handlers. Modifiez le bootstrap pour injecter un `FakeLogger` en test et un vrai `logging.Logger` en production. Vérifiez que l'introspection de `_call_handler` le résout correctement.

!!! example "Exercice 2 -- Tester l'injection"
    Écrivez un test qui vérifie que si un handler déclare un paramètre `inconnu` qui n'est pas dans les dépendances, le bus le gère proprement (que se passe-t-il actuellement ?).

!!! example "Exercice 3 -- Container DI"
    Installez `dependency-injector` et réécrivez le bootstrap en utilisant un container. Comparez le nombre de lignes, la lisibilité et la facilité de debug avec le bootstrap manuel.

---

!!! tip "À retenir"
    - Un handler ne crée **jamais** ses dépendances. Il les reçoit.
    - Le **bootstrap** est le seul endroit où les implémentations concrètes
      sont instanciées.
    - L'**introspection** (`inspect.signature`) permet au bus d'injecter
      automatiquement les bonnes dépendances dans chaque handler.
    - En tests, on remplace les implémentations concrètes par des **fakes**
      via le même mécanisme de bootstrap.
    - La DI manuelle via un bootstrap explicite est souvent **préférée** en
      Python à un framework DI. N'ajoutez de la complexité que quand elle
      est justifiée.
