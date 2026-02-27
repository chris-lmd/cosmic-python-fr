# Chapitre 8 -- Events et le Message Bus

> **Pattern** : Domain Events + Message Bus
> **Probleme resolu** : Comment reagir aux changements du domaine sans coupler la logique metier aux effets de bord ?

---

## Le probleme des effets de bord

Jusqu'ici, notre systeme d'allocation fait bien son travail : il choisit le bon batch,
respecte les regles metier, et le Unit of Work garantit l'atomicite des transactions.

Mais la realite rattrape vite une architecture trop simple. Quand une allocation reussit,
il faut probablement :

- **Envoyer un email** de confirmation au client
- **Mettre a jour un tableau de bord** en temps reel
- **Notifier un service externe** (entrepot, logistique, facturation)
- **Emettre un evenement** vers un bus de messages (Redis, Kafka...)

La tentation naturelle est de tout mettre dans le handler :

```python
# NE FAITES PAS CA -- handler monolithique
def allocate(cmd, uow):
    line = OrderLine(cmd.orderid, cmd.sku, cmd.qty)
    with uow:
        product = uow.products.get(sku=cmd.sku)
        batchref = product.allocate(line)
        uow.commit()

    # Effets de bord empiles...
    send_email("client@example.com", f"Commande {cmd.orderid} allouee")
    update_dashboard(cmd.sku, batchref)
    notify_warehouse(batchref, cmd.orderid)
    publish_to_redis("allocation", {"orderid": cmd.orderid})
    return batchref
```

Ce code pose trois problemes serieux :

1. **Couplage** : le handler connait les details de chaque systeme externe. Si on ajoute
   un nouveau consommateur, il faut modifier le handler.
2. **Testabilite** : pour tester l'allocation, il faut mocker l'email, le dashboard,
   l'entrepot et Redis. Les tests deviennent fragiles et lents.
3. **Responsabilite** : le handler orchestre la logique metier *et* gere les effets de
   bord. C'est une violation du Single Responsibility Principle.

La solution ? Separer le **fait** (une allocation a eu lieu) de ses **consequences**
(envoyer un email, notifier un service). C'est exactement ce que permettent les
Domain Events.

---

## Les Domain Events

Un Domain Event est un objet qui represente **un fait qui s'est produit** dans le
systeme. Pas une demande, pas une intention -- un constat irrevocable.

La convention de nommage est importante : les events sont toujours au **passe** :

| Event | Signification |
|-------|---------------|
| `Allocated` | Une ligne de commande **a ete** allouee a un batch |
| `Deallocated` | Une ligne de commande **a ete** desallouee |
| `OutOfStock` | Le stock **est epuise** pour un SKU donne |

Comparez avec les commands, qui sont des **demandes** au present imperatif :

| Command | Event correspondant |
|---------|---------------------|
| `Allocate` (alloue !) | `Allocated` (a ete alloue) |
| `ChangeBatchQuantity` (change !) | `Deallocated` (a ete desalloue) |

Cette distinction est fondamentale. Une command peut echouer (stock insuffisant,
SKU inexistant). Un event, lui, **ne peut pas echouer** : il decrit un fait deja
survenu. On ne peut pas "refuser" qu'une allocation ait eu lieu.

---

## La structure d'un Event

Les events sont definis dans `src/allocation/domain/events.py`. La structure repose
sur une classe de base `Event` et des dataclasses concretes avec `frozen=True`.

### La classe de base

```python
class Event:
    """Classe de base pour tous les events du domaine."""
    pass
```

Volontairement minimale, elle sert de **marqueur de type** : elle permet au message bus
de distinguer un event d'une command. Pas de methode, pas d'attribut -- juste un contrat.

### Les events concrets

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Allocated(Event):
    """Un OrderLine a ete alloue a un Batch."""
    orderid: str
    sku: str
    qty: int
    batchref: str

@dataclass(frozen=True)
class Deallocated(Event):
    """Un OrderLine a ete desalloue d'un Batch."""
    orderid: str
    sku: str
    qty: int

@dataclass(frozen=True)
class OutOfStock(Event):
    """Le stock est epuise pour un SKU donne."""
    sku: str
```

### Pourquoi `frozen=True` ?

Le parametre `frozen=True` rend la dataclass **immutable**. C'est un choix delibere :

- Un event represente un **fait passe**. On ne modifie pas le passe.
- L'immutabilite garantit qu'un handler ne peut pas corrompre un event avant
  qu'un autre handler ne le traite.
- Un objet frozen est **hashable**, utilisable dans des sets ou comme cle de dict.

```python
event = Allocated(orderid="o1", sku="LAMP", qty=10, batchref="batch-001")
event.qty = 5  # FrozenInstanceError !
```

Chaque event porte exactement les **donnees necessaires** pour que ses handlers puissent
travailler sans dependre du contexte d'emission. `Allocated` porte l'`orderid`, le `sku`,
la `qty` et le `batchref`. `OutOfStock` ne porte que le `sku`. Un event est
**autosuffisant** : ses consommateurs n'ont pas besoin d'interroger la base de donnees.

---

## Les agregats emettent des events

C'est le domaine qui sait quand quelque chose d'interessant se produit. Pas le handler,
pas la couche service -- le **domaine lui-meme**. C'est donc l'agregat qui emet les events.

L'agregat `Product` maintient une liste d'events en attente :

```python
class Product:
    def __init__(self, sku: str, batches=None, version_number: int = 0):
        self.sku = sku
        self.batches = batches or []
        self.version_number = version_number
        self.events: list[events.Event] = []  # (1)
```

**(1)** La liste `self.events` est le **tampon d'events**. Les events y sont accumules
pendant l'execution des methodes metier, puis collectes par le Unit of Work.

### Emission lors de l'allocation

Quand l'allocation echoue par manque de stock, le `Product` emet un `OutOfStock` :

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

**(1)** Plutot que de lever une exception, le domaine **enregistre un fait** : "le stock
est epuise". C'est un changement de philosophie important.

### Emission lors du changement de quantite

Quand la quantite d'un batch diminue, chaque desallocation genere un `Deallocated` :

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

Si trois lignes sont desallouees, il y aura trois events `Deallocated`, et chacun pourra
declencher une reallocation independante.

### Pourquoi le domaine et pas le handler ?

On pourrait etre tente de faire emettre les events par le handler. Mais ce serait une
erreur :

- Le **domaine connait les regles**. C'est lui qui sait qu'une rupture de stock s'est
  produite. Le handler ne fait que transmettre la demande.
- Si un autre handler appelle la meme methode, les events sont emis **automatiquement**.
- On garde le domaine comme **source de verite** pour les faits metier.

---

## Le Message Bus

Les events sont emis par le domaine, mais ils ne servent a rien s'ils restent dans une
liste. Il faut un mecanisme pour les **router vers les bons handlers**. C'est le role
du Message Bus.

### La structure

Le Message Bus est un **dispatcher** defini dans
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

Le bus recoit a la construction le **Unit of Work**, un dictionnaire
**event_handlers** (chaque event vers une *liste* de handlers), un dictionnaire
**command_handlers** (chaque command vers *un* handler), et des **dependances** a
injecter (notifications, etc.).

### Le point d'entree : `handle()`

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

Le coeur du bus est cette boucle `while`, qui fonctionne comme une **file de messages** :

1. Le message initial est place dans la queue
2. Tant qu'il reste des messages, on depile et on traite
3. Chaque traitement peut generer de **nouveaux events** ajoutes a la queue
4. La boucle continue jusqu'a epuisement

C'est cette mecanique qui permet la **propagation en cascade** : une command peut
declencher un event, qui declenche un handler, qui produit un autre event.

### Events vs. Commands : une asymetrie deliberee

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
| Nombre de handlers | Exactement **un** | **Zero, un ou plusieurs** |
| En cas d'erreur | L'exception **remonte** | L'exception est **logguee**, les autres handlers continuent |
| Valeur de retour | Peut retourner un resultat | Pas de retour attendu |

Une **command** est une demande explicite -- si elle echoue, l'appelant doit le savoir.
Un **event** est un constat -- si l'envoi d'email echoue, ce n'est pas une raison pour
annuler l'allocation.

### La collecte des events par le Unit of Work

Apres chaque handler, le bus appelle `self.uow.collect_new_events()` :

```python
class AbstractUnitOfWork(abc.ABC):
    def collect_new_events(self):
        for product in self.products.seen:
            while product.events:
                yield product.events.pop(0)
```

Le UoW itere sur tous les agregats **vus** pendant la transaction, vide leur liste
d'events, et les transmet au bus. C'est le pont entre le domaine et l'infrastructure.

---

## Les Event Handlers concrets

Voyons les handlers d'events definis dans `src/allocation/service_layer/handlers.py`.

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

Ce handler recoit un `OutOfStock` et une dependance `notifications` injectee par le bus.
Pas besoin du UoW, pas besoin de la base -- juste l'event et l'adapter.

### Reallocation apres desallocation

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

Quand une ligne est desallouee, ce handler cree une **nouvelle command** `Allocate` et
la traite. C'est une **chaine reactive** : un changement de quantite entraine des
desallocations, qui entrainent des reallocations.

### Le cablage dans le bootstrap

Les associations sont declarees dans `src/allocation/service_layer/bootstrap.py` :

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

Pour ajouter un nouveau comportement, il suffit d'**ajouter un handler a la liste** --
sans modifier le code existant. C'est le principe Open/Closed en action :

```python
events.OutOfStock: [
    handlers.send_out_of_stock_notification,  # email existant
    handlers.send_out_of_stock_sms,            # SMS (nouveau !)
],
```

---

## Le flux complet

Deroulons un scenario de bout en bout : un changement de quantite de batch qui
declenche une cascade d'events.

### Scenario : la quantite d'un batch diminue

Un fournisseur informe qu'un batch de 50 "BLUE-VASE" ne contiendra que 20 unites.
Trois commandes de 10 etaient allouees. Avec 20 unites, une doit etre desallouee.

**Etape 1** -- La command entre dans le bus :

```python
cmd = ChangeBatchQuantity(ref="batch-001", qty=20)
bus.handle(cmd)
```

**Etape 2** -- Le command handler charge le Product et appelle la methode metier.

**Etape 3** -- Le domaine desalloue une ligne et emet `Deallocated(orderid="order-3",
sku="BLUE-VASE", qty=10)`.

**Etape 4** -- Le UoW collecte l'event et l'ajoute a la queue du bus.

**Etape 5** -- Le bus depile le `Deallocated` et appelle le handler `reallocate`.

**Etape 6** -- `reallocate` cree une command `Allocate` et la traite. Le systeme
tente de reallouer la ligne a un autre batch.

**Etape 7** -- Si la reallocation reussit, un `Allocated` est emis. S'il n'y a plus de
stock, un `OutOfStock` declenche l'envoi d'une notification. La boucle continue
jusqu'a epuisement de la queue.

### Visualisation du flux

```
ChangeBatchQuantity (command)
    |
    v
change_batch_quantity (command handler)
    |
    v
Product.change_batch_quantity() --- emet ---> Deallocated (event)
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
                                        succes            echec
                                          |                |
                                          v                v
                                     Allocated         OutOfStock
                                          |                |
                                          v                v
                                  publish_event     send_notification
```

---

## Resume

Les Domain Events et le Message Bus resolvent le probleme des effets de bord en
**decouplant les faits de leurs consequences**.

### Les concepts cles

| Concept | Role |
|---------|------|
| **Domain Event** | Objet immutable representant un fait passe |
| **`self.events`** | Tampon dans l'agregat ou les events sont accumules |
| **Message Bus** | Dispatcher qui route events et commands vers leurs handlers |
| **`collect_new_events()`** | Methode du UoW qui extrait les events des agregats |
| **Event Handler** | Fonction qui reagit a un event |

### Les regles a retenir

1. **Les events sont au passe** : `Allocated`, pas `Allocate`. Ils constatent, ils ne
   demandent pas.
2. **Les events sont immutables** : `frozen=True`. Le passe ne change pas.
3. **Le domaine emet les events** : c'est l'agregat qui sait ce qui s'est passe, pas
   le handler.
4. **Un event peut avoir plusieurs handlers** : ajouter un comportement ne necessite pas
   de modifier le code existant.
5. **Un handler d'event ne fait pas echouer les autres** : les erreurs sont logguees
   mais la propagation continue.
6. **La boucle du bus traite en cascade** : un handler peut generer de nouveaux events,
   qui sont traites a leur tour.

### Schema d'architecture

```
                    +------------------+
                    |   Point d'entree |
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

Avec les Domain Events et le Message Bus, notre architecture gagne en **extensibilite**
(ajouter des reactions sans modifier le domaine), en **testabilite** (chaque handler se
teste independamment) et en **clarte** (chaque composant a une responsabilite unique).

Dans le chapitre suivant, nous verrons comment le Message Bus peut devenir le **point
d'entree principal** de toute l'application, en remplacement de la service layer
traditionnelle.
