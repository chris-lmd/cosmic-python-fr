# Chapitre 9 -- Aller plus loin avec le Message Bus

## Le Message Bus comme cœur de l'architecture

Dans les chapitres précédents, le message bus était un mécanisme secondaire :
l'API appelait directement les service layer handlers, et le bus servait
uniquement à propager les events en tant que side-effects. Cette approche
fonctionnait, mais elle créait une asymétrie gênante : les commands et les
events empruntaient des chemins différents dans l'application.

L'idée centrale de ce chapitre est simple mais transformatrice : **tout passe
par le bus**. Le message bus n'est plus un outil annexe -- il devient le point
d'entrée unique de l'application. Toute opération transite par le même
pipeline, qu'elle soit déclenchée par une requête HTTP, un message Redis ou un
event interne. Conséquences :

- **Uniformité** : commands et events suivent le même chemin de dispatch.
- **Découplage** : l'API ne connaît plus les handlers, seulement le bus.
- **Extensibilité** : ajouter un comportement = ajouter un handler.

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

## Avant / Après : l'évolution du point d'entrée

### Avant : l'API appelle directement les handlers

Dans une architecture classique, le endpoint Flask aurait ressemblé à ceci :

```python
@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    line = OrderLine(data["orderid"], data["sku"], data["qty"])
    batchref = services.allocate(line, unit_of_work.SqlAlchemyUnitOfWork())
    return jsonify({"batchref": batchref}), 201
```

L'API connaissait les fonctions du service layer et instanciait elle-même les
dépendances. Après, dans `src/allocation/entrypoints/flask_app.py` :

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

Le endpoint ne connaît plus aucun handler. Son travail se résume à :

1. Extraire les données de la requête HTTP.
2. Construire un objet `Command`.
3. Le soumettre au `MessageBus` via `bus.handle(cmd)`.
4. Convertir le résultat en réponse HTTP.

C'est un **thin adapter** au sens propre : une fine couche de traduction entre
le protocole HTTP et le langage interne du domaine (les commands). Toute la
configuration -- quels handlers répondent à quels messages, quelles dépendances
sont injectées -- est définie dans le bootstrap, pas dans l'API.

---

## La file d'attente interne

Le cœur du mécanisme réside dans la méthode `handle()` du `MessageBus` et
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

1. Le message initial (en général une `Command`) est placé dans `self.queue`.
2. La boucle `while self.queue` dépile les messages un par un.
3. Chaque message est dispatché vers le handler correspondant.
4. Après l'exécution d'un handler, les events émis par les agrégats sont
   collectés via `self.uow.collect_new_events()` et ajoutés à la queue.
5. La boucle continue jusqu'à ce que la queue soit vide.

Ce mécanisme est visible dans `_handle_command` et `_handle_event` :

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

**(1)** et **(2)** : après chaque exécution de handler, on collecte les events
du domaine et on les réinjecte dans la queue. C'est ce qui permet la
propagation en cascade. La méthode `collect_new_events` du Unit of Work
parcourt tous les agrégats observés pendant la transaction :

```python
def collect_new_events(self):
    for product in self.products.seen:
        while product.events:
            yield product.events.pop(0)
```

### Différence de traitement entre commands et events

| Aspect             | Command                          | Event                                                  |
|--------------------|----------------------------------|--------------------------------------------------------|
| Nombre de handlers | Exactement 1                     | 0, 1 ou N                                             |
| En cas d'erreur    | L'exception remonte à l'appelant | L'exception est loggée, les autres handlers continuent |
| Valeur de retour   | Oui (ajoutée à `results`)        | Non                                                    |

Une command est une intention qui **doit** aboutir ou échouer explicitement.
Un event est une notification qui ne doit pas bloquer le flux principal.

---

## Handlers en cascade

Le vrai intérêt de la queue interne apparaît quand les handlers déclenchent
eux-mêmes de nouveaux events. Prenons un scénario concret.

### Scénario : réduction de la quantité d'un lot

Un fournisseur nous informe qu'un lot de 50 unités ne contiendra finalement
que 25 unités. Certaines lignes de commande déjà allouées à ce lot doivent
être désallouées puis réallouées à d'autres lots.

```text
1. ChangeBatchQuantity (command)
   --> change_batch_quantity handler
       --> Product.change_batch_quantity()
           --> émet Deallocated event(s)
2. Deallocated (event) ajouté à la queue
   --> reallocate handler
       --> Product.allocate()
           --> peut émettre OutOfStock
3. (optionnel) OutOfStock (event) ajouté à la queue
   --> send_out_of_stock_notification handler
       --> envoie un email via l'adapter de notifications
```

Dans le domaine (`src/allocation/domain/model.py`), le modèle émet les events
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

Côté handlers (`src/allocation/service_layer/handlers.py`), `reallocate`
réagit à l'event `Deallocated` :

```python
def reallocate(event: events.Deallocated, uow: AbstractUnitOfWork) -> None:
    allocate(
        commands.Allocate(orderid=event.orderid, sku=event.sku, qty=event.qty),
        uow=uow,
    )
```

Et si `allocate()` échoue par manque de stock, le domaine émet un `OutOfStock`
event, dispatché vers `send_out_of_stock_notification` :

```python
def send_out_of_stock_notification(
    event: events.OutOfStock, notifications: AbstractNotifications,
) -> None:
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )
```

Personne n'a eu besoin d'orchestrer cette cascade. **Le comportement émerge de
la composition des handlers**, pas d'un code d'orchestration central.

---

## L'injection de dépendances dans le bus

Les handlers ont besoin de dépendances (`uow`, `notifications`, etc.), mais on
ne veut pas que l'appelant ait à les fournir. La solution : le bus les injecte
automatiquement en inspectant la signature de chaque handler.

### La méthode `_call_handler`

```python
def _call_handler(self, handler: Callable, message: Message) -> Any:
    import inspect

    params = inspect.signature(handler).parameters
    kwargs: dict[str, Any] = {}
    for name, param in params.items():
        if name == list(params.keys())[0]:
            continue  # Premier paramètre = le message lui-même
        if name == "uow":
            kwargs[name] = self.uow
        elif name in self.dependencies:
            kwargs[name] = self.dependencies[name]

    return handler(message, **kwargs)
```

La logique est la suivante :

1. `inspect.signature(handler).parameters` extrait les paramètres du handler.
2. Le premier paramètre est toujours le message -- on le saute.
3. Pour chaque paramètre suivant, le bus cherche une correspondance :
    - `"uow"` : on injecte le Unit of Work.
    - Autre nom : on cherche dans `self.dependencies`.
4. Le handler est appelé avec le message en premier et les dépendances en
   keyword arguments.

Prenons `send_out_of_stock_notification(event, notifications)`. Le bus
inspecte la signature, trouve `"notifications"` dans `self.dependencies`, et
appelle `handler(event, notifications=email_adapter)`. Le handler n'a jamais
besoin de savoir d'où viennent ses dépendances.

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

La clé `"notifications"` dans le dictionnaire doit correspondre exactement au
nom du paramètre dans la signature du handler. **Le nom du paramètre fait
office de contrat**. Le mapping handlers/messages est déclaré explicitement :

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

## Résumé : le nouveau schéma d'architecture

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

### Principes clés

1. **Un seul point d'entrée** : tout passe par `bus.handle(message)`. Que
   l'appelant soit un endpoint Flask, un subscriber Redis ou un test unitaire,
   le chemin est identique.

2. **Propagation automatique** : les events émis par le domaine sont collectés
   et traités sans intervention. Aucun code d'orchestration n'est nécessaire.

3. **Injection par introspection** : le bus injecte les dépendances dans les
   handlers en inspectant leurs signatures. Les handlers déclarent ce dont ils
   ont besoin, le bus fournit.

4. **Séparation des responsabilités** :
    - Les **entrypoints** traduisent les entrées externes en commands.
    - Le **bus** dispatche et orchestre.
    - Les **handlers** contiennent la logique applicative.
    - Le **domaine** contient les règles métier et émet des events.

5. **Testabilité** : le bootstrap accepte des fakes pour chaque dépendance,
   rendant les tests rapides et isolés.

### Ce que nous avons gagné

| Avant                                            | Après                                                  |
|--------------------------------------------------|--------------------------------------------------------|
| L'API appelle les handlers directement           | L'API envoie des commands au bus                       |
| Les dépendances sont passées manuellement        | Les dépendances sont injectées automatiquement         |
| Les side-effects sont gérés à part               | Tout transite par le bus, commands comme events        |
| Ajouter un comportement = modifier du code       | Ajouter un handler + l'enregistrer dans le bootstrap   |
| Tests couplés aux détails d'implémentation       | Tests via le bus avec des fakes injectées              |

Le message bus est devenu la colonne vertébrale de l'application. Toute
l'intelligence est dans les handlers et le domaine ; le bus ne fait que
distribuer les messages et injecter les dépendances. Cette simplicité apparente
cache une grande puissance : on peut ajouter des comportements complexes
(cascades d'events, notifications, publication externe) sans jamais modifier
le code existant.
