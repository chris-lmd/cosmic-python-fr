# Chapitre 9 -- Le Message Bus

> **Pattern** : Message Bus
> **Problème résolu** : Comment distribuer Commands et Events vers les bons handlers, gérer les cascades, et injecter les dépendances ?

---

## Le cœur de l'architecture

Aux chapitres 7 et 8, nous avons défini deux types de messages : les **Commands** (intentions) et les **Events** (faits). Mais nous n'avons pas encore montré **comment** ces messages arrivent aux bons handlers. C'est le rôle du **Message Bus**.

Le Message Bus est le point central de notre architecture. **Tout passe par lui** :

```
                         ┌──────────────────────────┐
                         │                          │
  FastAPI (HTTP) ────────>│                          │──> ajouter_lot()
                         │                          │──> allouer()
  Redis (events) ──────>│      Message Bus          │──> modifier_quantite_lot()
                         │                          │──> réallouer()
  Tests ────────────────>│                          │──> envoyer_notification()
                         │                          │──> ajouter_allocation_vue()
                         │                          │
                         └──────────────────────────┘
```

L'API FastAPI ne connaît pas les handlers. Les handlers ne connaissent pas FastAPI. Le bus est l'intermédiaire qui découple tout.

---

## La classe `MessageBus`

Voici la classe complète, telle qu'elle apparaît dans `src/allocation/service_layer/messagebus.py` :

```python
import inspect
import logging
from typing import Any, Callable, Union

from allocation.domain import commands, events
from allocation.service_layer import unit_of_work

logger = logging.getLogger(__name__)

Message = Union[commands.Command, events.Event]


class MessageBus:

    def __init__(
        self,
        uow: unit_of_work.AbstractUnitOfWork,
        event_handlers: dict[type[events.Event], list[Callable]],
        command_handlers: dict[type[commands.Command], Callable],
        dependencies: dict[str, Any] | None = None,
    ):
        self.uow = uow
        self.event_handlers = event_handlers
        self.command_handlers = command_handlers
        self.dependencies = dependencies or {}
        self.queue: list[Message] = []

    def handle(self, message: Message) -> list[Any]:
        self.queue = [message]
        results: list[Any] = []
        while self.queue:
            message = self.queue.pop(0)
            if isinstance(message, events.Event):
                self._handle_event(message)
            elif isinstance(message, commands.Command):
                result = self._handle_command(message)
                results.append(result)
            else:
                raise ValueError(f"Message de type inconnu : {type(message)}")
        return results

    def _handle_event(self, event: events.Event) -> None:
        for handler in self.event_handlers.get(type(event), []):
            try:
                logger.debug("Traitement de l'event %s avec %s", event, handler)
                self._call_handler(handler, event)
                self.queue.extend(self.uow.collect_new_events())
            except Exception:
                logger.exception("Erreur lors du traitement de l'event %s", event)

    def _handle_command(self, command: commands.Command) -> Any:
        logger.debug("Traitement de la command %s", command)
        handler = self.command_handlers.get(type(command))
        if handler is None:
            raise ValueError(f"Aucun handler pour la command {type(command)}")
        result = self._call_handler(handler, command)
        self.queue.extend(self.uow.collect_new_events())
        return result

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

Décomposons chaque partie.

---

## `handle()` : le point d'entrée et la queue

```python
def handle(self, message: Message) -> list[Any]:
    self.queue = [message]
    results: list[Any] = []
    while self.queue:
        message = self.queue.pop(0)
        if isinstance(message, events.Event):
            self._handle_event(message)
        elif isinstance(message, commands.Command):
            result = self._handle_command(message)
            results.append(result)
        else:
            raise ValueError(f"Message de type inconnu : {type(message)}")
    return results
```

Le principe est simple :

1. Le message initial est placé dans une **queue** (file d'attente).
2. La boucle `while` traite les messages un par un, en FIFO (`pop(0)`).
3. Chaque handler peut émettre de nouveaux events (via `collect_new_events()`), qui sont ajoutés à la fin de la queue.
4. La boucle continue jusqu'à ce que la queue soit vide.

C'est cette boucle qui permet les **cascades** : un event peut déclencher un handler, qui modifie un agrégat, qui émet un nouvel event, qui déclenche un autre handler, et ainsi de suite.

Le type `Message = Union[commands.Command, events.Event]` permet au bus d'accepter indifféremment les deux types. Le dispatch se fait par `isinstance`.

---

## `_handle_command()` : strict, un seul handler

```python
def _handle_command(self, command: commands.Command) -> Any:
    handler = self.command_handlers.get(type(command))
    if handler is None:
        raise ValueError(f"Aucun handler pour la command {type(command)}")
    result = self._call_handler(handler, command)
    self.queue.extend(self.uow.collect_new_events())
    return result
```

Trois points clés :

1. **Un seul handler** par command. Le dictionnaire mappe un type de command à une seule fonction. Si le handler n'existe pas, c'est une `ValueError`.
2. **L'exception remonte** directement à l'appelant. Pas de `try/except` ici -- contrairement aux events. Si `allouer` lève `SkuInconnu`, FastAPI reçoit l'exception et retourne une erreur 400.
3. **Le résultat est retourné**. Par exemple, `allouer` retourne la référence du lot choisi, que FastAPI inclut dans la réponse JSON.
4. **Les events sont collectés** après l'exécution du handler et ajoutés à la queue pour traitement ultérieur.

---

## `_handle_event()` : tolérant, N handlers

```python
def _handle_event(self, event: events.Event) -> None:
    for handler in self.event_handlers.get(type(event), []):
        try:
            self._call_handler(handler, event)
            self.queue.extend(self.uow.collect_new_events())
        except Exception:
            logger.exception("Erreur lors du traitement de l'event %s", event)
```

Les différences avec `_handle_command()` sont délibérées (voir chapitre 8) :

1. **Plusieurs handlers** possibles. Le dictionnaire mappe un type d'event à une **liste** de fonctions.
2. **Tolérance aux pannes** : chaque handler est dans son propre `try/except`. Si l'envoi d'email échoue, la mise à jour du read model continue quand même.
3. **Pas de retour**. Un event est un fait broadcast -- personne n'attend de résultat.
4. **Les events sont collectés après chaque handler**, pas seulement à la fin de tous. Cela garantit que les events émis par un handler sont disponibles pour les suivants.

---

## `_call_handler()` : injection de dépendances par introspection

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

Cette méthode est la clé de l'**injection de dépendances**. Elle fonctionne ainsi :

1. **Introspection** : `inspect.signature(handler).parameters` retourne un dictionnaire ordonné des paramètres du handler.
2. **Premier paramètre** : c'est toujours le message (command ou event), passé en positional. On le saute.
3. **Paramètres suivants** : résolus par nom.
   - Si le paramètre s'appelle `uow`, on injecte `self.uow`.
   - Sinon, on cherche dans `self.dependencies`.

Concrètement, pour le handler `envoyer_notification_rupture_stock` :

```python
def envoyer_notification_rupture_stock(
    event: events.RuptureDeStock,       # <-- 1er param, passé en positional
    notifications: AbstractNotifications,  # <-- résolu par nom dans dependencies
) -> None:
```

Le bus voit que le second paramètre s'appelle `notifications`, trouve cette clé dans `self.dependencies`, et injecte l'objet correspondant (`EmailNotifications` en production, `FakeNotifications` en test). Le handler n'a aucune idée de la provenance de ses dépendances. Nous verrons comment ces dépendances sont assemblées au [chapitre 10](chapitre_10_bootstrap_di.md).

---

## Les dictionnaires de routage

Le bus reçoit deux dictionnaires qui associent chaque type de message à son ou ses handlers. Ces dictionnaires sont définis dans le **Composition Root** (`bootstrap.py`) et passés au bus lors de sa construction -- nous les détaillerons au [chapitre 10](chapitre_10_bootstrap_di.md). Ils ont été présentés au [chapitre 8](chapitre_08_commands.md) pour illustrer la distinction command/event.

Le principe est simple : ajouter un nouveau handler pour un event existant revient à ajouter une entrée dans une liste. Ajouter un nouveau type de command revient à ajouter une clé dans le dictionnaire. **Aucun code existant n'est modifié** -- c'est le principe Open/Closed.

---

## FastAPI comme adaptateur mince

Avec le Message Bus en place, FastAPI devient un simple **adaptateur HTTP vers Commands** :

```python
from allocation.domain import commands
from allocation.service_layer import bootstrap, handlers

app = FastAPI()
bus = bootstrap.bootstrap()


@app.post("/add_batch", status_code=201)
def add_batch_endpoint(data: dict):
    eta = data.get("eta")
    if eta is not None:
        eta = datetime.fromisoformat(eta).date()

    cmd = commands.CréerLot(
        réf=data["ref"], sku=data["sku"], quantité=data["qty"], eta=eta,
    )
    bus.handle(cmd)
    return "OK"


@app.post("/allocate", status_code=201)
def allocate_endpoint(data: dict):
    try:
        cmd = commands.Allouer(
            id_commande=data["orderid"], sku=data["sku"], quantité=data["qty"],
        )
        results = bus.handle(cmd)
        réf_lot = results.pop(0)
    except handlers.SkuInconnu as e:
        return {"message": str(e)}
    return {"batchref": réf_lot}


@app.get("/allocations/{id_commande}")
def allocations_view_endpoint(id_commande: str):
    from allocation.views import views
    result = views.allocations(id_commande, bus.uow)
    if not result:
        return "not found"
    return result
```

Chaque endpoint fait trois choses et rien de plus :

1. **Désérialise** la requête HTTP en une Command (ou appelle une view pour les lectures).
2. **Envoie** la Command au bus via `bus.handle(cmd)`.
3. **Sérialise** le résultat en réponse HTTP.

Aucune logique métier, aucun appel direct aux handlers, aucune connaissance du domaine. Si demain on remplace FastAPI par une CLI, seule cette couche change.

---

## Scénario de cascade : `ModifierQuantitéLot`

Le scénario le plus intéressant est celui de la modification de quantité, qui déclenche une cascade d'events :

```
1. ModifierQuantitéLot(réf="lot-001", quantité=25)        [COMMAND]
   │
   │  handler: modifier_quantité_lot()
   │  -> produit.modifier_quantité_lot("lot-001", 25)
   │  -> l'agrégat désalloue les lignes en excédent
   │  -> émet Désalloué(id_commande="cmd-001", sku="CHAISE", quantité=20)
   │
   v
2. Désalloué(id_commande="cmd-001", sku="CHAISE", quantité=20)    [EVENT]
   │
   ├── handler 1: réallouer()
   │   -> appelle allouer(Allouer(...), uow)
   │   -> l'agrégat alloue la ligne à un autre lot
   │   -> émet Alloué(id_commande="cmd-001", sku="CHAISE", réf_lot="lot-002")
   │
   ├── handler 2: supprimer_allocation_vue()
   │   -> DELETE FROM allocations_view WHERE ...
   │
   v
3. Alloué(id_commande="cmd-001", sku="CHAISE", réf_lot="lot-002")  [EVENT]
   │
   ├── handler 1: publier_événement_allocation()
   │   -> log l'allocation
   │
   ├── handler 2: ajouter_allocation_vue()
   │   -> INSERT INTO allocations_view ...
   │
   v
   (queue vide, fin)
```

Voici le même flux sous forme de diagramme de séquence :

```
┌───────┐    ┌───────────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│  Bus  │    │modifier_quan- │    │ Produit  │    │réallouer │    │ allouer  │
│       │    │ tité_lot()    │    │(agrégat) │    │          │    │          │
└───┬───┘    └──────┬────────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘
    │               │                  │               │               │
    │ Command       │                  │               │               │
    │──────────────>│                  │               │               │
    │               │  modifier_       │               │               │
    │               │  quantité_lot()  │               │               │
    │               │─────────────────>│               │               │
    │               │                  │ émet          │               │
    │               │                  │ Désalloué     │               │
    │               │<─────────────────│               │               │
    │               │                  │               │               │
    │  collect_new_events()            │               │               │
    │  => [Désalloué]                  │               │               │
    │               │                  │               │               │
    │ Event: Désalloué                 │               │               │
    │──────────────────────────────────────────────────>│               │
    │               │                  │               │  allouer()    │
    │               │                  │               │──────────────>│
    │               │                  │               │               │
    │  collect_new_events()            │               │               │
    │  => [Alloué]                     │               │               │
    │               │                  │               │               │
    │ Event: Alloué                    │               │               │
    │ -> publier_événement_allocation()│               │               │
    │ -> ajouter_allocation_vue()      │               │               │
    │               │                  │               │               │
    │ (queue vide)  │                  │               │               │
    v               v                  v               v               v
```

Tout cela se passe dans un **seul appel** à `bus.handle(ModifierQuantitéLot(...))`. L'appelant envoie une Command et récupère le résultat. Il ne sait rien des events intermédiaires, des réallocations, ni des mises à jour du read model.

---

## Tester avec le bus

Pour tester le système complet (commands, events, cascades), on utilise une fonction `bootstrap_test_bus()` qui assemble le bus avec des fakes :

```python
class FakeRepository(AbstractRepository):
    def __init__(self, produits=None):
        super().__init__()
        self._produits = set(produits or [])

    def _add(self, produit):
        self._produits.add(produit)

    def _get(self, sku):
        return next((p for p in self._produits if p.sku == sku), None)

    def _get_par_réf_lot(self, réf_lot):
        return next(
            (p for p in self._produits for l in p.lots if l.référence == réf_lot),
            None,
        )


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    def __init__(self):
        self.produits = FakeRepository()

    def __enter__(self):
        return super().__enter__()

    def _commit(self):
        pass

    def rollback(self):
        pass


class FakeNotifications(AbstractNotifications):
    def __init__(self):
        self.envoyées: list[tuple[str, str]] = []

    def send(self, destination, message):
        self.envoyées.append((destination, message))


def bootstrap_test_bus(uow=None, notifications=None):
    """Même wiring que la production, mais avec des fakes."""
    if uow is None:
        uow = FakeUnitOfWork()
    if notifications is None:
        notifications = FakeNotifications()
    return bootstrap.bootstrap(
        start_orm=False,
        uow=uow,
        notifications_adapter=notifications,
    )
```

Le point important : `bootstrap_test_bus()` appelle le **même** `bootstrap.bootstrap()` que la production. Les dictionnaires EVENT_HANDLERS et COMMAND_HANDLERS sont identiques. On teste le vrai wiring avec de fausses dépendances.

### Exemple : tester la cascade de réallocation

```python
class TestModifierQuantitéLot:
    def test_réalloue_si_quantité_réduite(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("lot-001", "CHAISE-BLEUE", 50, None))
        bus.handle(commands.CréerLot("lot-002", "CHAISE-BLEUE", 50, None))
        bus.handle(commands.Allouer("cmd-001", "CHAISE-BLEUE", 20))
        bus.handle(commands.Allouer("cmd-002", "CHAISE-BLEUE", 20))

        # Réduction de lot-001 : 40 allouées, mais seulement 25 de capacité
        bus.handle(commands.ModifierQuantitéLot("lot-001", 25))

        produit = bus.uow.produits.get("CHAISE-BLEUE")
        lot_001 = next(l for l in produit.lots if l.référence == "lot-001")
        lot_002 = next(l for l in produit.lots if l.référence == "lot-002")
        # Le total alloué reste 40, réparti entre les deux lots
        assert lot_001.quantité_allouée + lot_002.quantité_allouée == 40
```

Ce test vérifie la cascade complète :
`ModifierQuantitéLot` -> `Désalloué` -> `réallouer` -> `Alloué`.
Tout passe par le bus, exactement comme en production.

### Exemple : tester la notification de rupture de stock

```python
class TestNotificationRuptureDeStock:
    def test_envoie_notification_si_rupture(self):
        notifications = FakeNotifications()
        bus = bootstrap_test_bus(notifications=notifications)
        bus.handle(commands.CréerLot("lot-001", "LAMPE-RARE", 10, None))
        bus.handle(commands.Allouer("cmd-001", "LAMPE-RARE", 10))

        bus.handle(commands.Allouer("cmd-002", "LAMPE-RARE", 1))

        assert len(notifications.envoyées) == 1
        assert "LAMPE-RARE" in notifications.envoyées[0][1]
```

On injecte un `FakeNotifications` pour capturer les emails. Le test vérifie la cascade :
`Allouer` -> `RuptureDeStock` -> `envoyer_notification_rupture_stock`.

---

## Le bus comme seul point d'entrée

Avec le Message Bus, l'architecture prend cette forme :

```
┌──────────────┐     ┌──────────────────────────────────────────────┐
│              │     │              Message Bus                     │
│   FastAPI    │     │                                              │
│   (HTTP)     │────>│  handle(Command)                             │
│              │     │      │                                       │
└──────────────┘     │      v                                       │
                     │  command_handler(cmd, uow, ...)              │
┌──────────────┐     │      │                                       │
│              │     │      │ collect_new_events()                   │
│   Redis      │     │      v                                       │
│   (events)   │────>│  event_handler_1(event, uow, ...)           │
│              │     │  event_handler_2(event, notifications, ...)  │
└──────────────┘     │      │                                       │
                     │      │ collect_new_events()                   │
┌──────────────┐     │      v                                       │
│              │     │  ... (cascade jusqu'à queue vide)            │
│   Tests      │────>│                                              │
│              │     │  return results                              │
└──────────────┘     └──────────────────────────────────────────────┘
```

Les avantages de cette architecture :

| Avantage | Explication |
|----------|------------|
| **Découplage** | Les points d'entrée (FastAPI, Redis, tests) ne connaissent pas les handlers. |
| **Extensibilité** | Ajouter un handler = ajouter une entrée dans un dictionnaire. Open/Closed. |
| **Testabilité** | Le même bus avec des fakes permet de tester toute la logique sans I/O. |
| **Cascade** | Les events déclenchent automatiquement de nouvelles actions via la queue. |
| **Injection** | Les dépendances sont injectées automatiquement par introspection des signatures. |

---

## Résumé

| Concept | Ce qu'il faut retenir |
|---------|----------------------|
| **MessageBus** | Le point central qui distribue commands et events aux bons handlers. |
| **Queue** | Les messages sont traités en FIFO. Les nouveaux events sont ajoutés à la fin. |
| **`_handle_command`** | Un handler, exception propagée, résultat retourné. |
| **`_handle_event`** | N handlers, exceptions loggées, pas de retour. |
| **`_call_handler`** | Injection de dépendances par introspection de la signature du handler. |
| **Cascade** | Un handler peut émettre des events qui déclenchent d'autres handlers, en chaîne. |
| **FastAPI adaptateur** | L'API convertit HTTP en Commands et les envoie au bus. Zéro logique métier. |
| **`bootstrap_test_bus()`** | Assemble le bus avec des fakes. Même wiring que la production. |

---

## Exercices

!!! example "Exercice 1 -- Tracer la cascade"
    Ajoutez des `print()` dans `handle()`, `_handle_command()` et `_handle_event()` pour tracer l'ordre de traitement des messages. Envoyez une `ModifierQuantitéLot` qui provoque une désallocation et observez la cascade complète dans la sortie.

!!! example "Exercice 2 -- Event sans handler"
    Que se passe-t-il si un event est émis mais n'a aucun handler dans `EVENT_HANDLERS` ? Vérifiez dans le code de `_handle_event()`. Écrivez un test qui émet un event orphelin et vérifiez qu'aucune erreur n'est levée.

!!! example "Exercice 3 -- Command sans handler"
    Que se passe-t-il si on envoie une Command inconnue au bus ? Écrivez un test qui le vérifie. Est-ce le bon comportement ?

!!! example "Exercice 4 -- Protection contre les boucles"
    Imaginez un event handler qui émet le même type d'event qu'il traite. Que se passerait-il ? Implémentez un compteur de profondeur dans `handle()` qui lève une exception après 100 itérations pour protéger contre les boucles infinies.

---

!!! quote "À retenir"
    Le Message Bus transforme l'application en une architecture réactive où tout est message. Les Commands entrent, les Events cascadent, et les handlers réagissent. Le bus est le seul point d'entrée -- FastAPI, Redis, et les tests ne sont que des adaptateurs qui fabriquent des messages et les envoient au bus.

---

*Chapitre suivant : [Bootstrap et injection de dépendances](chapitre_10_bootstrap_di.md) -- comment assembler tous les composants proprement.*
