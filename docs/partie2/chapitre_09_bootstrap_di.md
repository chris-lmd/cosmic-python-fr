# Chapitre 9 -- Bootstrap et injection de dépendances

> **Pattern** : Dependency Injection + Composition Root
> **Problème résolu** : Comment assembler tous les composants (handlers, UoW, notifications, bus) sans créer de couplage entre eux ?

---

## Le problème : qui crée les dépendances ?

Nos handlers ont besoin de collaborateurs pour fonctionner. Le handler `allouer` a besoin d'un `AbstractUnitOfWork`. Le handler `envoyer_notification_rupture_stock` a besoin d'un `AbstractNotifications`. Mais **qui fournit ces objets ?**

### Option 1 : le handler crée ses propres dépendances (mauvais)

```python
# NE FAITES PAS ÇA
def allouer(cmd):
    uow = SqlAlchemyUnitOfWork()  # couplage direct !
    with uow:
        produit = uow.produits.get(sku=cmd.sku)
        ...
```

Problèmes :
- Le handler est **couplé** à l'implémentation concrète `SqlAlchemyUnitOfWork`.
- Impossible de tester avec un `FakeUnitOfWork` sans monkey-patching.
- Si on veut changer de base de données, il faut modifier chaque handler.

### Option 2 : le handler reçoit ses dépendances (bon)

```python
# INJECTION DE DÉPENDANCES
def allouer(cmd, uow: AbstractUnitOfWork):
    with uow:
        produit = uow.produits.get(sku=cmd.sku)
        ...
```

Le handler déclare ce dont il a besoin dans sa **signature**. Quelqu'un d'autre se charge de fournir l'objet concret. C'est le principe de l'**Injection de Dépendances** (Dependency Injection, DI).

La question devient alors : **qui est ce "quelqu'un d'autre" ?**

---

## Le Composition Root : `bootstrap.py`

La réponse est le **Composition Root** -- un endroit unique dans l'application où toutes les dépendances sont assemblées. C'est notre fichier `src/allocation/service_layer/bootstrap.py` :

```python
from allocation.adapters import notifications, orm
from allocation.domain import commands, events
from allocation.service_layer import handlers, messagebus, unit_of_work


def bootstrap(
    start_orm: bool = True,
    uow: unit_of_work.AbstractUnitOfWork | None = None,
    notifications_adapter: notifications.AbstractNotifications | None = None,
    **extra_dependencies,
) -> messagebus.MessageBus:
    """
    Construit et retourne un MessageBus configuré.

    En production, utilise les implémentations concrètes.
    En test, on injecte des fakes via les paramètres.
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

### Ce que fait `bootstrap()` :

1. **Démarre le mapping ORM** si nécessaire (`start_orm=True` en production, `False` en test).
2. **Crée les implémentations concrètes** si aucune n'est injectée :
   - `SqlAlchemyUnitOfWork` pour la base de données
   - `EmailNotifications` pour les emails
3. **Assemble le dictionnaire de dépendances** qui sera utilisé par l'injection automatique.
4. **Construit le MessageBus** avec le UoW, les dictionnaires de routage, et les dépendances.

### Le pattern "defaults concrets, fakes injectables"

La signature de `bootstrap()` utilise un pattern puissant :

```python
def bootstrap(
    uow: AbstractUnitOfWork | None = None,           # None = production
    notifications_adapter: AbstractNotifications | None = None,  # None = production
):
    if uow is None:
        uow = SqlAlchemyUnitOfWork()      # default = concret
    if notifications_adapter is None:
        notifications_adapter = EmailNotifications()   # default = concret
```

- **En production** : on appelle `bootstrap()` sans arguments. Les implémentations concrètes sont utilisées.
- **En test** : on appelle `bootstrap(uow=fake_uow, notifications_adapter=fake_notif)`. Les fakes sont injectées.

C'est simple, explicite, et ne nécessite aucun framework de DI.

---

## Les dictionnaires de routage complets

Le Composition Root est aussi l'endroit où le **routage des messages** est défini. Voici les dictionnaires complets :

### EVENT_HANDLERS

```python
EVENT_HANDLERS: dict[type[events.Event], list] = {
    events.Alloué: [
        handlers.publier_événement_allocation,
        handlers.ajouter_allocation_vue,
    ],
    events.Désalloué: [
        handlers.réallouer,
        handlers.supprimer_allocation_vue,
    ],
    events.RuptureDeStock: [
        handlers.envoyer_notification_rupture_stock,
    ],
}
```

Chaque event est mappé à une **liste** de handlers :

| Event | Handlers | Effets |
|-------|---------|--------|
| `Alloué` | `publier_événement_allocation` | Log l'allocation (placeholder pour Redis/Kafka) |
| | `ajouter_allocation_vue` | Insère dans le read model CQRS (chapitre 11) |
| `Désalloué` | `réallouer` | Crée une commande `Allouer` pour réallouer la ligne |
| | `supprimer_allocation_vue` | Supprime du read model CQRS |
| `RuptureDeStock` | `envoyer_notification_rupture_stock` | Envoie un email à l'équipe stock |

### COMMAND_HANDLERS

```python
COMMAND_HANDLERS: dict[type[commands.Command], Any] = {
    commands.CréerLot: handlers.ajouter_lot,
    commands.Allouer: handlers.allouer,
    commands.ModifierQuantitéLot: handlers.modifier_quantité_lot,
}
```

Chaque command est mappée à **un seul** handler :

| Command | Handler | Effet |
|---------|---------|-------|
| `CréerLot` | `ajouter_lot` | Crée un lot (et le produit si nécessaire) |
| `Allouer` | `allouer` | Alloue une ligne au meilleur lot disponible |
| `ModifierQuantitéLot` | `modifier_quantité_lot` | Modifie la quantité d'un lot (peut déclencher des désallocations) |

---

## L'injection automatique : `_call_handler()`

Le mécanisme d'injection est au cœur du Message Bus (chapitre 9). Revistons-le en détail :

```python
def _call_handler(self, handler: Callable, message: Message) -> Any:
    params = inspect.signature(handler).parameters
    kwargs: dict[str, Any] = {}
    for name, param in params.items():
        if name == list(params.keys())[0]:
            continue  # premier paramètre = le message
        if name == "uow":
            kwargs[name] = self.uow
        elif name in self.dependencies:
            kwargs[name] = self.dependencies[name]
    return handler(message, **kwargs)
```

### Comment ça marche, pas à pas

Prenons le handler `envoyer_notification_rupture_stock` :

```python
def envoyer_notification_rupture_stock(
    event: events.RuptureDeStock,       # paramètre 1 : le message
    notifications: AbstractNotifications, # paramètre 2 : une dépendance
) -> None:
    notifications.send(...)
```

1. **`inspect.signature(handler).parameters`** retourne :
   ```
   OrderedDict([
       ('event', <Parameter "event: events.RuptureDeStock">),
       ('notifications', <Parameter "notifications: AbstractNotifications">),
   ])
   ```

2. **Premier paramètre** (`event`) : c'est le message. On le saute -- il sera passé en positional.

3. **Deuxième paramètre** (`notifications`) :
   - Ce n'est pas `uow`, donc on cherche dans `self.dependencies`.
   - `self.dependencies["notifications"]` existe (il a été mis là par `bootstrap()`).
   - On l'ajoute aux kwargs.

4. **Appel final** : `handler(event, notifications=<EmailNotifications>)`.

### Autre exemple : un handler avec `uow`

```python
def allouer(cmd: commands.Allouer, uow: AbstractUnitOfWork) -> str:
    ...
```

1. Premier paramètre (`cmd`) : le message, passé en positional.
2. Deuxième paramètre (`uow`) : le nom est `uow`, donc on injecte `self.uow` directement.
3. Appel : `handler(cmd, uow=<SqlAlchemyUnitOfWork>)`.

### Le UoW est traité à part

Notez que le `uow` n'est pas dans le dictionnaire `dependencies` : il est géré séparément car le MessageBus en a besoin pour `collect_new_events()`. C'est un cas spécial justifié par le rôle central du UoW dans le cycle de vie des events.

---

## Le bootstrap de test

Dans les tests, on utilise une fonction helper qui appelle le même `bootstrap()` mais avec des fakes :

```python
# tests/unit/test_handlers.py

def bootstrap_test_bus(uow=None, notifications=None):
    """Même wiring que la production, mais avec des fakes."""
    if uow is None:
        uow = FakeUnitOfWork()
    if notifications is None:
        notifications = FakeNotifications()
    return bootstrap.bootstrap(
        start_orm=False,         # pas de mapping ORM
        uow=uow,                # FakeUnitOfWork au lieu de SqlAlchemy
        notifications_adapter=notifications,  # FakeNotifications au lieu de SMTP
    )
```

Le diagramme de comparaison :

```
PRODUCTION                              TEST
┌────────────────────────┐              ┌────────────────────────┐
│ bootstrap()            │              │ bootstrap(             │
│                        │              │   start_orm=False,     │
│ ORM: start_mappers()   │              │   uow=FakeUnitOfWork(),│
│ UoW: SqlAlchemyUoW     │              │   notifications=       │
│ Notif: EmailNotif      │              │     FakeNotifications()│
│                        │              │ )                      │
│ dependencies = {       │              │ dependencies = {       │
│   "notifications":     │              │   "notifications":     │
│     EmailNotifications │              │     FakeNotifications  │
│ }                      │              │ }                      │
│                        │              │                        │
│ MessageBus(            │              │ MessageBus(            │
│   uow=SqlAlchemyUoW,  │              │   uow=FakeUoW,        │
│   event_handlers=...,  │              │   event_handlers=...,  │  <-- IDENTIQUES
│   command_handlers=...,│              │   command_handlers=...,│  <-- IDENTIQUES
│   dependencies=...     │              │   dependencies=...     │
│ )                      │              │ )                      │
└────────────────────────┘              └────────────────────────┘
```

Les dictionnaires de routage (`EVENT_HANDLERS`, `COMMAND_HANDLERS`) sont **identiques** en production et en test. Seules les **implémentations concrètes** diffèrent. C'est la garantie que les tests vérifient le vrai wiring.

---

## Pourquoi pas un framework de DI ?

En Java ou C#, l'injection de dépendances passe généralement par un **conteneur IoC** (Inversion of Control) : Spring, Autofac, etc. Pourquoi ne pas utiliser l'équivalent Python (comme `dependency-injector` ou `inject`) ?

### Les arguments contre un framework

1. **Complexité inutile.** Notre `bootstrap()` fait 20 lignes. Un framework de DI ajouterait de la configuration, des décorateurs, et une courbe d'apprentissage.

2. **Python a déjà les outils.** Les fonctions sont des first-class citizens, les closures capturent le contexte, `inspect.signature()` donne l'introspection. Pas besoin de magie supplémentaire.

3. **Lisibilité.** Avec `bootstrap()`, un nouveau développeur voit immédiatement comment les dépendances sont assemblées. Avec un framework, il doit comprendre le framework avant de comprendre l'application.

### Les arguments pour un framework

1. **Gestion du cycle de vie.** Un framework gère les singletons, les scopes (par requête, par session), et le nettoyage automatique.

2. **Applications de grande taille.** Quand il y a 50 dépendances et 200 handlers, le bootstrap manuel devient pénible.

3. **Découverte automatique.** Certains frameworks détectent automatiquement les handlers et les dépendances.

### Notre choix : DI manuelle

Pour notre application (3 commands, 3 events, 5 dépendances), la DI manuelle est largement suffisante. Le `bootstrap()` est lisible, testable, et ne dépend d'aucune librairie externe.

La règle : **commencez simple, complexifiez si nécessaire**. Quand le `bootstrap()` dépasse 100 lignes, c'est peut-être le moment de considérer un framework.

---

## Le diagramme de câblage complet

Voici comment tous les composants s'assemblent :

```
bootstrap()
    │
    ├── Crée SqlAlchemyUnitOfWork (ou FakeUnitOfWork)
    │       │
    │       └── Contient SqlAlchemyRepository (ou FakeRepository)
    │               │
    │               └── Expose .seen pour collect_new_events()
    │
    ├── Crée EmailNotifications (ou FakeNotifications)
    │
    ├── Assemble dependencies = {"notifications": <notif>}
    │
    ├── Utilise EVENT_HANDLERS = {
    │       Alloué: [publier_événement_allocation, ajouter_allocation_vue],
    │       Désalloué: [réallouer, supprimer_allocation_vue],
    │       RuptureDeStock: [envoyer_notification_rupture_stock],
    │   }
    │
    ├── Utilise COMMAND_HANDLERS = {
    │       CréerLot: ajouter_lot,
    │       Allouer: allouer,
    │       ModifierQuantitéLot: modifier_quantité_lot,
    │   }
    │
    └── Retourne MessageBus(uow, event_handlers, command_handlers, dependencies)
            │
            ├── handle(message) : point d'entrée unique
            ├── _handle_command() : strict, 1 handler
            ├── _handle_event() : tolérant, N handlers
            └── _call_handler() : injection par introspection
```

Et voici comment les différents points d'entrée utilisent le bus :

```
┌───────────────┐
│  fastapi_app  │  bus = bootstrap.bootstrap()
│               │  bus.handle(commands.CréerLot(...))
│               │  bus.handle(commands.Allouer(...))
└───────────────┘

┌───────────────┐
│   tests       │  bus = bootstrap_test_bus()  # fakes injectées
│               │  bus.handle(commands.CréerLot(...))
│               │  bus.handle(commands.Allouer(...))
└───────────────┘

┌───────────────┐
│   consumer    │  bus = bootstrap.bootstrap()
│   (Redis)     │  bus.handle(commands.CréerLot(...))
└───────────────┘
```

Trois points d'entrée différents, **le même bus**, **le même wiring**. Seules les implémentations concrètes changent.

---

## Résumé

| Concept | Ce qu'il faut retenir |
|---------|----------------------|
| **Injection de dépendances** | Les handlers déclarent leurs besoins dans leur signature. Quelqu'un d'autre fournit les objets. |
| **Composition Root** | `bootstrap.py` est l'endroit unique où toutes les dépendances sont assemblées. |
| **`bootstrap()`** | Crée le UoW, les notifications, le dictionnaire de dépendances, et le MessageBus. |
| **`_call_handler()`** | Injecte les dépendances par introspection de la signature du handler. |
| **`bootstrap_test_bus()`** | Même `bootstrap()` avec des fakes. Même routage, implémentations différentes. |
| **DI manuelle vs framework** | En Python, la DI manuelle est souvent suffisante. Commencez simple. |

---

## Exercices

!!! example "Exercice 1 -- Nouvelle dépendance"
    Ajoutez une dépendance `logger` au bootstrap. Créez un handler `log_allocation(event: Alloué, logger: Logger)` qui logge les allocations. Vérifiez que l'injection fonctionne en test avec un `FakeLogger`.

!!! example "Exercice 2 -- Dependency manquante"
    Que se passe-t-il si un handler déclare un paramètre (`metrics: AbstractMetrics`) qui n'est pas dans le dictionnaire de dépendances ? Testez et expliquez le comportement. Comment pourrait-on améliorer le message d'erreur ?

!!! example "Exercice 3 -- Bootstrap avec framework"
    Installez `dependency-injector` et réécrivez `bootstrap.py` en utilisant un conteneur IoC. Comparez la lisibilité et la testabilité avec la version manuelle.

!!! example "Exercice 4 -- extra_dependencies"
    Le paramètre `**extra_dependencies` de `bootstrap()` permet d'ajouter des dépendances supplémentaires. Écrivez un test qui injecte une dépendance `audit_log` via ce mécanisme et vérifiez qu'un handler peut la recevoir.

---

!!! quote "À retenir"
    Le bootstrap est le point névralgique de l'application : c'est le seul endroit qui connaît les implémentations concrètes. Tout le reste du code travaille avec des abstractions. En production, `bootstrap()` crée les vrais objets. En test, on injecte des fakes. Le routage (EVENT_HANDLERS, COMMAND_HANDLERS) reste identique dans les deux cas -- on teste le vrai câblage avec de fausses dépendances.

---

*Fin de la Partie 2. Consultez l'[épilogue](../epilogue.md) pour un récapitulatif de l'architecture complète.*
