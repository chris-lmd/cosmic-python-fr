# Chapitre 9 -- Aller plus loin avec le Message Bus

## Le Message Bus comme coeur de l'architecture

Dans les chapitres precedents, le message bus etait un mecanisme secondaire :
l'API appelait directement les service layer handlers, et le bus servait
uniquement a propager les events en tant que side-effects. Cette approche
fonctionnait, mais elle creait une asymetrie genante : les commands et les
events empruntaient des chemins differents dans l'application.

L'idee centrale de ce chapitre est simple mais transformatrice : **tout passe
par le bus**. Le message bus n'est plus un outil annexe -- il devient le point
d'entree unique de l'application. Toute operation transite par le meme
pipeline, qu'elle soit declenchee par une requete HTTP, un message Redis ou un
event interne. Consequences :

- **Uniformite** : commands et events suivent le meme chemin de dispatch.
- **Decouplage** : l'API ne connait plus les handlers, seulement le bus.
- **Extensibilite** : ajouter un comportement = ajouter un handler.

```text
                  +-----------+
  HTTP Request -->|  Flask    |
                  |  (thin    |---> Command ---> MessageBus
                  |  adapter) |                     |
                  +-----------+                     |
                                            +-------+-------+
                                            |               |
                                      Command           Event
                                      Handler           Handlers
                                            |               |
                                            v               v
                                          UoW           UoW / Adapters
```

---

## Avant / Apres : l'evolution du point d'entree

### Avant : l'API appelle directement les handlers

Dans une architecture classique, le endpoint Flask aurait ressemble a ceci :

```python
@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    line = OrderLine(data["orderid"], data["sku"], data["qty"])
    batchref = services.allocate(line, unit_of_work.SqlAlchemyUnitOfWork())
    return jsonify({"batchref": batchref}), 201
```

L'API connaissait les fonctions du service layer et instanciait elle-meme les
dependances. Apres, dans `src/allocation/entrypoints/flask_app.py` :

```python
bus = bootstrap.bootstrap()

@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    try:
        cmd = commands.Allocate(
            orderid=data["orderid"],
            sku=data["sku"],
            qty=data["qty"],
        )
        results = bus.handle(cmd)
        batchref = results.pop(0)
    except handlers.InvalidSku as e:
        return jsonify({"message": str(e)}), 400

    return jsonify({"batchref": batchref}), 201
```

Le endpoint ne connait plus aucun handler. Son travail se resume a :

1. Extraire les donnees de la requete HTTP.
2. Construire un objet `Command`.
3. Le soumettre au `MessageBus` via `bus.handle(cmd)`.
4. Convertir le resultat en reponse HTTP.

C'est un **thin adapter** au sens propre : une fine couche de traduction entre
le protocole HTTP et le langage interne du domaine (les commands). Toute la
configuration -- quels handlers repondent a quels messages, quelles dependances
sont injectees -- est definie dans le bootstrap, pas dans l'API.

---

## La file d'attente interne

Le coeur du mecanisme reside dans la methode `handle()` du `MessageBus` et
dans son attribut `self.queue` (`src/allocation/service_layer/messagebus.py`) :

```python
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
```

Le fonctionnement est le suivant :

1. Le message initial (en general une `Command`) est place dans `self.queue`.
2. La boucle `while self.queue` depile les messages un par un.
3. Chaque message est dispatche vers le handler correspondant.
4. Apres l'execution d'un handler, les events emis par les agregats sont
   collectes via `self.uow.collect_new_events()` et ajoutes a la queue.
5. La boucle continue jusqu'a ce que la queue soit vide.

Ce mecanisme est visible dans `_handle_command` et `_handle_event` :

```python
def _handle_command(self, command: commands.Command) -> Any:
    handler = self.command_handlers.get(type(command))
    if handler is None:
        raise ValueError(f"Aucun handler pour la command {type(command)}")
    result = self._call_handler(handler, command)
    self.queue.extend(self.uow.collect_new_events())  # (1)
    return result

def _handle_event(self, event: events.Event) -> None:
    for handler in self.event_handlers.get(type(event), []):
        try:
            self._call_handler(handler, event)
            self.queue.extend(self.uow.collect_new_events())  # (2)
        except Exception:
            logger.exception("Erreur lors du traitement de l'event %s", event)
```

**(1)** et **(2)** : apres chaque execution de handler, on collecte les events
du domaine et on les reinjecte dans la queue. C'est ce qui permet la
propagation en cascade. La methode `collect_new_events` du Unit of Work
parcourt tous les agregats observes pendant la transaction :

```python
def collect_new_events(self):
    for product in self.products.seen:
        while product.events:
            yield product.events.pop(0)
```

### Difference de traitement entre commands et events

| Aspect             | Command                          | Event                                                  |
|--------------------|----------------------------------|--------------------------------------------------------|
| Nombre de handlers | Exactement 1                     | 0, 1 ou N                                             |
| En cas d'erreur    | L'exception remonte a l'appelant | L'exception est loggee, les autres handlers continuent |
| Valeur de retour   | Oui (ajoutee a `results`)        | Non                                                    |

Une command est une intention qui **doit** aboutir ou echouer explicitement.
Un event est une notification qui ne doit pas bloquer le flux principal.

---

## Handlers en cascade

Le vrai interet de la queue interne apparait quand les handlers declenchent
eux-memes de nouveaux events. Prenons un scenario concret.

### Scenario : reduction de la quantite d'un lot

Un fournisseur nous informe qu'un lot de 50 unites ne contiendra finalement
que 25 unites. Certaines lignes de commande deja allouees a ce lot doivent
etre desallouees puis reallouees a d'autres lots.

```text
1. ChangeBatchQuantity (command)
   --> change_batch_quantity handler
       --> Product.change_batch_quantity()
           --> emet Deallocated event(s)
2. Deallocated (event) ajoute a la queue
   --> reallocate handler
       --> Product.allocate()
           --> peut emettre OutOfStock
3. (optionnel) OutOfStock (event) ajoute a la queue
   --> send_out_of_stock_notification handler
       --> envoie un email via l'adapter de notifications
```

Dans le domaine (`src/allocation/domain/model.py`), le modele emet les events
sans savoir ce qui va se passer ensuite :

```python
def change_batch_quantity(self, ref: str, qty: int) -> None:
    batch = next(b for b in self.batches if b.reference == ref)
    batch._purchased_quantity = qty
    while batch.available_quantity < 0:
        line = batch.deallocate_one()
        self.events.append(
            events.Deallocated(orderid=line.orderid, sku=line.sku, qty=line.qty)
        )
```

Cote handlers (`src/allocation/service_layer/handlers.py`), `reallocate`
reagit a l'event `Deallocated` :

```python
def reallocate(event: events.Deallocated, uow: AbstractUnitOfWork) -> None:
    allocate(
        commands.Allocate(orderid=event.orderid, sku=event.sku, qty=event.qty),
        uow=uow,
    )
```

Et si `allocate()` echoue par manque de stock, le domaine emet un `OutOfStock`
event, dispatche vers `send_out_of_stock_notification` :

```python
def send_out_of_stock_notification(
    event: events.OutOfStock, notifications: AbstractNotifications,
) -> None:
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )
```

Personne n'a eu besoin d'orchestrer cette cascade. **Le comportement emerge de
la composition des handlers**, pas d'un code d'orchestration central.

---

## L'injection de dependances dans le bus

Les handlers ont besoin de dependances (`uow`, `notifications`, etc.), mais on
ne veut pas que l'appelant ait a les fournir. La solution : le bus les injecte
automatiquement en inspectant la signature de chaque handler.

### La methode `_call_handler`

```python
def _call_handler(self, handler: Callable, message: Message) -> Any:
    import inspect

    params = inspect.signature(handler).parameters
    kwargs: dict[str, Any] = {}
    for name, param in params.items():
        if name == list(params.keys())[0]:
            continue  # Premier parametre = le message lui-meme
        if name == "uow":
            kwargs[name] = self.uow
        elif name in self.dependencies:
            kwargs[name] = self.dependencies[name]

    return handler(message, **kwargs)
```

La logique est la suivante :

1. `inspect.signature(handler).parameters` extrait les parametres du handler.
2. Le premier parametre est toujours le message -- on le saute.
3. Pour chaque parametre suivant, le bus cherche une correspondance :
    - `"uow"` : on injecte le Unit of Work.
    - Autre nom : on cherche dans `self.dependencies`.
4. Le handler est appele avec le message en premier et les dependances en
   keyword arguments.

Prenons `send_out_of_stock_notification(event, notifications)`. Le bus
inspecte la signature, trouve `"notifications"` dans `self.dependencies`, et
appelle `handler(event, notifications=email_adapter)`. Le handler n'a jamais
besoin de savoir d'ou viennent ses dependances.

### Le bootstrap : la composition root

L'assemblage se fait dans `src/allocation/service_layer/bootstrap.py` :

```python
def bootstrap(
    start_orm: bool = True,
    uow: unit_of_work.AbstractUnitOfWork | None = None,
    notifications_adapter: notifications.AbstractNotifications | None = None,
    **extra_dependencies: Any,
) -> messagebus.MessageBus:
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

La cle `"notifications"` dans le dictionnaire doit correspondre exactement au
nom du parametre dans la signature du handler. **Le nom du parametre fait
office de contrat**. Le mapping handlers/messages est declare explicitement :

```python
EVENT_HANDLERS = {
    events.Allocated: [handlers.publish_allocated_event],
    events.Deallocated: [handlers.reallocate],
    events.OutOfStock: [handlers.send_out_of_stock_notification],
}
COMMAND_HANDLERS = {
    commands.CreateBatch: handlers.add_batch,
    commands.Allocate: handlers.allocate,
    commands.ChangeBatchQuantity: handlers.change_batch_quantity,
}
```

Pour les tests, on injecte des fakes sans toucher au code de production :

```python
bus = bootstrap.bootstrap(
    start_orm=False,
    uow=FakeUnitOfWork(),
    notifications_adapter=FakeNotifications(),
)
bus.handle(commands.CreateBatch(ref="batch-001", sku="TABLE", qty=100))
```

---

## Resume : le nouveau schema d'architecture

```text
  Entrypoints             Service Layer              Domain
 (thin adapters)       (MessageBus + Handlers)       (Model)
 +--------------+     +--------------------+     +-------------+
 | Flask API    | cmd |    MessageBus      |     |  Product    |
 | Redis sub    |---->|  1. queue = [cmd]  |     |  Batch      |
 | CLI          |     |  2. dispatch       |     |  OrderLine  |
 +--------------+     |  3. collect events |     +------+------+
                      |  4. repeat         |            |
                      |  Handlers + Deps   |<-- events--+
                      +--------------------+
```

### Principes cles

1. **Un seul point d'entree** : tout passe par `bus.handle(message)`. Que
   l'appelant soit un endpoint Flask, un subscriber Redis ou un test unitaire,
   le chemin est identique.

2. **Propagation automatique** : les events emis par le domaine sont collectes
   et traites sans intervention. Aucun code d'orchestration n'est necessaire.

3. **Injection par introspection** : le bus injecte les dependances dans les
   handlers en inspectant leurs signatures. Les handlers declarent ce dont ils
   ont besoin, le bus fournit.

4. **Separation des responsabilites** :
    - Les **entrypoints** traduisent les entrees externes en commands.
    - Le **bus** dispatche et orchestre.
    - Les **handlers** contiennent la logique applicative.
    - Le **domaine** contient les regles metier et emet des events.

5. **Testabilite** : le bootstrap accepte des fakes pour chaque dependance,
   rendant les tests rapides et isoles.

### Ce que nous avons gagne

| Avant                                            | Apres                                                  |
|--------------------------------------------------|--------------------------------------------------------|
| L'API appelle les handlers directement           | L'API envoie des commands au bus                       |
| Les dependances sont passees manuellement        | Les dependances sont injectees automatiquement         |
| Les side-effects sont geres a part               | Tout transite par le bus, commands comme events        |
| Ajouter un comportement = modifier du code       | Ajouter un handler + l'enregistrer dans le bootstrap   |
| Tests couples aux details d'implementation       | Tests via le bus avec des fakes injectees              |

Le message bus est devenu la colonne vertebrale de l'application. Toute
l'intelligence est dans les handlers et le domaine ; le bus ne fait que
distribuer les messages et injecter les dependances. Cette simplicite apparente
cache une grande puissance : on peut ajouter des comportements complexes
(cascades d'events, notifications, publication externe) sans jamais modifier
le code existant.
