# Chapitre 8 -- Events et le Message Bus

> **Pattern** : Domain Events + Message Bus
> **Problème résolu** : Comment réagir aux changements du domaine sans coupler la logique métier aux effets de bord ?

---

## Le problème des effets de bord

Jusqu'ici, notre système d'allocation fait bien son travail : il choisit le bon batch,
respecte les règles métier, et le Unit of Work garantit l'atomicité des transactions.

Mais la réalité rattrape vite une architecture trop simple. Quand une allocation réussit,
il faut probablement :

- **Envoyer un email** de confirmation au client
- **Mettre à jour un tableau de bord** en temps réel
- **Notifier un service externe** (entrepôt, logistique, facturation)
- **Émettre un événement** vers un bus de messages (Redis, Kafka...)

La tentation naturelle est de tout mettre dans le handler :

```python
# NE FAITES PAS CA -- handler monolithique
def allocate(cmd, uow):
    line = OrderLine(cmd.orderid, cmd.sku, cmd.qty)
    with uow:
        product = uow.products.get(sku=cmd.sku)
        batchref = product.allocate(line)
        uow.commit()

    # Effets de bord empilés...
    send_email("client@example.com", f"Commande {cmd.orderid} allouée")
    update_dashboard(cmd.sku, batchref)
    notify_warehouse(batchref, cmd.orderid)
    publish_to_redis("allocation", {"orderid": cmd.orderid})
    return batchref
```

Ce code pose trois problèmes sérieux :

1. **Couplage** : le handler connaît les détails de chaque système externe. Si on ajoute
   un nouveau consommateur, il faut modifier le handler.
2. **Testabilité** : pour tester l'allocation, il faut mocker l'email, le dashboard,
   l'entrepôt et Redis. Les tests deviennent fragiles et lents.
3. **Responsabilité** : le handler orchestre la logique métier *et* gère les effets de
   bord. C'est une violation du Single Responsibility Principle.

La solution ? Séparer le **fait** (une allocation a eu lieu) de ses **conséquences**
(envoyer un email, notifier un service). C'est exactement ce que permettent les
Domain Events.

---

## Les Domain Events

Un Domain Event est un objet qui représente **un fait qui s'est produit** dans le
système. Pas une demande, pas une intention -- un constat irrévocable.

La convention de nommage est importante : les events sont toujours au **passé** :

| Event | Signification |
|-------|---------------|
| `Allocated` | Une ligne de commande **a été** allouée à un batch |
| `Deallocated` | Une ligne de commande **a été** désallouée |
| `OutOfStock` | Le stock **est épuisé** pour un SKU donné |

Comparez avec les commands, qui sont des **demandes** au présent impératif :

| Command | Event correspondant |
|---------|---------------------|
| `Allocate` (alloue !) | `Allocated` (a été alloué) |
| `ChangeBatchQuantity` (change !) | `Deallocated` (a été désalloué) |

Cette distinction est fondamentale. Une command peut échouer (stock insuffisant,
SKU inexistant). Un event, lui, **ne peut pas échouer** : il décrit un fait déjà
survenu. On ne peut pas "refuser" qu'une allocation ait eu lieu.

---

## La structure d'un Event

Les events sont définis dans `src/allocation/domain/events.py`. La structure repose
sur une classe de base `Event` et des dataclasses concrètes avec `frozen=True`.

### La classe de base

```python
class Event:
    """Classe de base pour tous les events du domaine."""
    pass
```

Volontairement minimale, elle sert de **marqueur de type** : elle permet au message bus
de distinguer un event d'une command. Pas de méthode, pas d'attribut -- juste un contrat.

### Les events concrets

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Allocated(Event):
    """Un OrderLine a été alloué à un Batch."""
    orderid: str
    sku: str
    qty: int
    batchref: str

@dataclass(frozen=True)
class Deallocated(Event):
    """Un OrderLine a été désalloué d'un Batch."""
    orderid: str
    sku: str
    qty: int

@dataclass(frozen=True)
class OutOfStock(Event):
    """Le stock est épuisé pour un SKU donné."""
    sku: str
```

### Pourquoi `frozen=True` ?

Le paramètre `frozen=True` rend la dataclass **immutable**. C'est un choix délibéré :

- Un event représente un **fait passé**. On ne modifie pas le passé.
- L'immutabilité garantit qu'un handler ne peut pas corrompre un event avant
  qu'un autre handler ne le traite.
- Un objet frozen est **hashable**, utilisable dans des sets ou comme clé de dict.

```python
event = Allocated(orderid="o1", sku="LAMP", qty=10, batchref="batch-001")
event.qty = 5  # FrozenInstanceError !
```

Chaque event porte exactement les **données nécessaires** pour que ses handlers puissent
travailler sans dépendre du contexte d'émission. `Allocated` porte l'`orderid`, le `sku`,
la `qty` et le `batchref`. `OutOfStock` ne porte que le `sku`. Un event est
**autosuffisant** : ses consommateurs n'ont pas besoin d'interroger la base de données.

---

## Les agrégats émettent des events

C'est le domaine qui sait quand quelque chose d'intéressant se produit. Pas le handler,
pas la couche service -- le **domaine lui-même**. C'est donc l'agrégat qui émet les events.

L'agrégat `Product` maintient une liste d'events en attente :

```python
class Product:
    def __init__(self, sku: str, batches=None, version_number: int = 0):
        self.sku = sku
        self.batches = batches or []
        self.version_number = version_number
        self.events: list[events.Event] = []  # (1)
```

**(1)** La liste `self.events` est le **tampon d'events**. Les events y sont accumulés
pendant l'exécution des méthodes métier, puis collectés par le Unit of Work.

### Émission lors de l'allocation

Quand l'allocation échoue par manque de stock, le `Product` émet un `OutOfStock` :

```python
def allocate(self, line: OrderLine) -> str:
    try:
        batch = next(
            b for b in sorted(self.batches) if b.can_allocate(line)
        )
    except StopIteration:
        self.events.append(events.OutOfStock(sku=line.sku))  # (1)
        return ""
    batch.allocate(line)
    self.version_number += 1
    return batch.reference
```

**(1)** Plutôt que de lever une exception, le domaine **enregistre un fait** : "le stock
est épuisé". C'est un changement de philosophie important.

### Émission lors du changement de quantité

Quand la quantité d'un batch diminue, chaque désallocation génère un `Deallocated` :

```python
def change_batch_quantity(self, ref: str, qty: int) -> None:
    batch = next(b for b in self.batches if b.reference == ref)
    batch._purchased_quantity = qty
    while batch.available_quantity < 0:
        line = batch.deallocate_one()
        self.events.append(
            events.Deallocated(
                orderid=line.orderid, sku=line.sku, qty=line.qty,
            )
        )
```

Si trois lignes sont désallouées, il y aura trois events `Deallocated`, et chacun pourra
déclencher une réallocation indépendante.

### Pourquoi le domaine et pas le handler ?

On pourrait être tenté de faire émettre les events par le handler. Mais ce serait une
erreur :

- Le **domaine connaît les règles**. C'est lui qui sait qu'une rupture de stock s'est
  produite. Le handler ne fait que transmettre la demande.
- Si un autre handler appelle la même méthode, les events sont émis **automatiquement**.
- On garde le domaine comme **source de vérité** pour les faits métier.

---

## Le Message Bus

Les events sont émis par le domaine, mais ils ne servent à rien s'ils restent dans une
liste. Il faut un mécanisme pour les **router vers les bons handlers**. C'est le rôle
du Message Bus.

### La structure

Le Message Bus est un **dispatcher** défini dans
`src/allocation/service_layer/messagebus.py` :

```python
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
```

Le bus reçoit à la construction le **Unit of Work**, un dictionnaire
**event_handlers** (chaque event vers une *liste* de handlers), un dictionnaire
**command_handlers** (chaque command vers *un* handler), et des **dépendances** à
injecter (notifications, etc.).

### Le point d'entrée : `handle()`

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

Le cœur du bus est cette boucle `while`, qui fonctionne comme une **file de messages** :

1. Le message initial est placé dans la queue
2. Tant qu'il reste des messages, on dépile et on traite
3. Chaque traitement peut générer de **nouveaux events** ajoutés à la queue
4. La boucle continue jusqu'à épuisement

C'est cette mécanique qui permet la **propagation en cascade** : une command peut
déclencher un event, qui déclenche un handler, qui produit un autre event.

### Events vs. Commands : une asymétrie délibérée

```python
def _handle_event(self, event: events.Event) -> None:
    for handler in self.event_handlers.get(type(event), []):
        try:
            self._call_handler(handler, event)
            self.queue.extend(self.uow.collect_new_events())
        except Exception:
            logger.exception("Erreur lors du traitement de l'event %s", event)

def _handle_command(self, command: commands.Command) -> Any:
    handler = self.command_handlers.get(type(command))
    if handler is None:
        raise ValueError(f"Aucun handler pour la command {type(command)}")
    result = self._call_handler(handler, command)
    self.queue.extend(self.uow.collect_new_events())
    return result
```

| Aspect | Command | Event |
|--------|---------|-------|
| Nombre de handlers | Exactement **un** | **Zéro, un ou plusieurs** |
| En cas d'erreur | L'exception **remonte** | L'exception est **logguée**, les autres handlers continuent |
| Valeur de retour | Peut retourner un résultat | Pas de retour attendu |

Une **command** est une demande explicite -- si elle échoue, l'appelant doit le savoir.
Un **event** est un constat -- si l'envoi d'email échoue, ce n'est pas une raison pour
annuler l'allocation.

### La collecte des events par le Unit of Work

Après chaque handler, le bus appelle `self.uow.collect_new_events()` :

```python
class AbstractUnitOfWork(abc.ABC):
    def collect_new_events(self):
        for product in self.products.seen:
            while product.events:
                yield product.events.pop(0)
```

Le UoW itère sur tous les agrégats **vus** pendant la transaction, vide leur liste
d'events, et les transmet au bus. C'est le pont entre le domaine et l'infrastructure.

---

## Les Event Handlers concrets

Voyons les handlers d'events définis dans `src/allocation/service_layer/handlers.py`.

### Notification de rupture de stock

```python
def send_out_of_stock_notification(
    event: events.OutOfStock,
    notifications: AbstractNotifications,
) -> None:
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )
```

Ce handler reçoit un `OutOfStock` et une dépendance `notifications` injectée par le bus.
Pas besoin du UoW, pas besoin de la base -- juste l'event et l'adapter.

### Réallocation après désallocation

```python
def reallocate(
    event: events.Deallocated,
    uow: AbstractUnitOfWork,
) -> None:
    allocate(
        commands.Allocate(
            orderid=event.orderid, sku=event.sku, qty=event.qty,
        ),
        uow=uow,
    )
```

Quand une ligne est désallouée, ce handler crée une **nouvelle command** `Allocate` et
la traite. C'est une **chaîne réactive** : un changement de quantité entraîne des
désallocations, qui entraînent des réallocations.

### Le câblage dans le bootstrap

Les associations sont déclarées dans `src/allocation/service_layer/bootstrap.py` :

```python
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

Pour ajouter un nouveau comportement, il suffit d'**ajouter un handler à la liste** --
sans modifier le code existant. C'est le principe Open/Closed en action :

```python
events.OutOfStock: [
    handlers.send_out_of_stock_notification,  # email existant
    handlers.send_out_of_stock_sms,            # SMS (nouveau !)
],
```

---

## Le flux complet

Déroulons un scénario de bout en bout : un changement de quantité de batch qui
déclenche une cascade d'events.

### Scénario : la quantité d'un batch diminue

Un fournisseur informe qu'un batch de 50 "BLUE-VASE" ne contiendra que 20 unités.
Trois commandes de 10 étaient allouées. Avec 20 unités, une doit être désallouée.

**Étape 1** -- La command entre dans le bus :

```python
cmd = ChangeBatchQuantity(ref="batch-001", qty=20)
bus.handle(cmd)
```

**Étape 2** -- Le command handler charge le Product et appelle la méthode métier.

**Étape 3** -- Le domaine désalloue une ligne et émet `Deallocated(orderid="order-3",
sku="BLUE-VASE", qty=10)`.

**Étape 4** -- Le UoW collecte l'event et l'ajoute à la queue du bus.

**Étape 5** -- Le bus dépile le `Deallocated` et appelle le handler `reallocate`.

**Étape 6** -- `reallocate` crée une command `Allocate` et la traite. Le système
tente de réallouer la ligne à un autre batch.

**Étape 7** -- Si la réallocation réussit, un `Allocated` est émis. S'il n'y a plus de
stock, un `OutOfStock` déclenche l'envoi d'une notification. La boucle continue
jusqu'à épuisement de la queue.

### Visualisation du flux

```
ChangeBatchQuantity (command)
    |
    v
change_batch_quantity (command handler)
    |
    v
Product.change_batch_quantity() --- émet ---> Deallocated (event)
    |                                              |
    v                                              v
UoW.commit()                              reallocate (event handler)
                                                   |
                                                   v
                                           Allocate (command)
                                                   |
                                                   v
                                           Product.allocate()
                                              /          \
                                        succès            échec
                                          |                |
                                          v                v
                                     Allocated         OutOfStock
                                          |                |
                                          v                v
                                  publish_event     send_notification
```

---

## Résumé

Les Domain Events et le Message Bus résolvent le problème des effets de bord en
**découplant les faits de leurs conséquences**.

### Les concepts clés

| Concept | Rôle |
|---------|------|
| **Domain Event** | Objet immutable représentant un fait passé |
| **`self.events`** | Tampon dans l'agrégat où les events sont accumulés |
| **Message Bus** | Dispatcher qui route events et commands vers leurs handlers |
| **`collect_new_events()`** | Méthode du UoW qui extrait les events des agrégats |
| **Event Handler** | Fonction qui réagit à un event |

### Les règles à retenir

1. **Les events sont au passé** : `Allocated`, pas `Allocate`. Ils constatent, ils ne
   demandent pas.
2. **Les events sont immutables** : `frozen=True`. Le passé ne change pas.
3. **Le domaine émet les events** : c'est l'agrégat qui sait ce qui s'est passé, pas
   le handler.
4. **Un event peut avoir plusieurs handlers** : ajouter un comportement ne nécessite pas
   de modifier le code existant.
5. **Un handler d'event ne fait pas échouer les autres** : les erreurs sont logguées
   mais la propagation continue.
6. **La boucle du bus traite en cascade** : un handler peut générer de nouveaux events,
   qui sont traités à leur tour.

### Schéma d'architecture

```
                    +------------------+
                    |   Point d'entrée |
                    |  (API / CLI)     |
                    +--------+---------+
                             |
                        Command
                             |
                             v
                    +------------------+
                    |   Message Bus    |<-----------+
                    +--------+---------+            |
                             |                      |
                    +--------+---------+    nouveaux events
                    | Command Handler  |            |
                    +--------+---------+            |
                             |                      |
                             v                      |
                    +------------------+            |
                    |     Domaine      |            |
                    | (Product.events) |            |
                    +--------+---------+            |
                             |                      |
                             v                      |
                    +------------------+            |
                    |   Unit of Work   |            |
                    | collect_events() +------------+
                    +--------+---------+
                             |
                             v
                    +------------------+
                    | Event Handlers   |
                    | (email, realloc, |
                    |  publish, ...)   |
                    +------------------+
```

Avec les Domain Events et le Message Bus, notre architecture gagne en **extensibilité**
(ajouter des réactions sans modifier le domaine), en **testabilité** (chaque handler se
teste indépendamment) et en **clarté** (chaque composant a une responsabilité unique).

Dans le chapitre suivant, nous verrons comment le Message Bus peut devenir le **point
d'entrée principal** de toute l'application, en remplacement de la service layer
traditionnelle.
