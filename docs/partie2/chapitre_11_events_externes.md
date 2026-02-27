# Chapitre 11 -- Events externes et communication entre services

!!! abstract "Ce que vous allez apprendre"
    - La difference entre events internes et events externes
    - Comment publier des events vers un message broker (Redis Pub/Sub)
    - Comment consommer des events externes et les convertir en commands internes
    - Le pattern Outbox pour garantir la publication fiable des events
    - L'importance de l'idempotence dans les consumers

---

## Events internes vs events externes

Jusqu'ici, nos events circulent uniquement **a l'interieur** de notre application.
Quand un `Product` emet un event `Allocated`, le message bus le dispatch vers
`publish_allocated_event` ou `reallocate` -- tout cela dans le meme processus.
Mais d'autres services ont besoin de savoir qu'une allocation a eu lieu, et des
systemes externes doivent pouvoir nous informer de changements. Comment faire
communiquer ces services sans les coupler ?

| Aspect | Event interne | Event externe |
|--------|--------------|---------------|
| **Perimetre** | Au sein d'un seul processus | Entre plusieurs services |
| **Transport** | Message bus en memoire | Message broker (Redis, RabbitMQ, Kafka...) |
| **Format** | Objets Python (dataclasses) | Donnees serialisees (JSON, Protobuf...) |
| **Fiabilite** | Garantie par le processus | Necessite des mecanismes dedies |
| **Couplage** | Meme bounded context | Entre bounded contexts differents |

Un event **interne** comme `Deallocated` declenche une reallocation dans le meme
service -- c'est un detail d'implementation invisible de l'exterieur. Un event
**externe** comme `Allocated` informe les autres services qu'une allocation a
eu lieu -- c'est un **contrat** dont d'autres equipes dependent.

```
┌──────────────────────────────────────┐
│       Service d'allocation           │
│                                      │
│  Command ──▶ Handler ──▶ Model       │
│                            │         │
│                    ┌───────▼──────┐  │
│                    │ Message Bus  │  │
│                    └───────┬──────┘  │
│                    Event Handler     │
│                    (publish)         │
└────────────────────────────┼─────────┘
                             │ Event externe (JSON)
                   ┌─────────▼─────────┐
                   │  Message Broker   │
                   └──┬─────────────┬──┘
            ┌─────────▼──┐   ┌──────▼────────┐
            │ Expedition │   │ Facturation   │
            └────────────┘   └───────────────┘
```

Les events externes offrent un **decouplage temporel et spatial** : le producteur
ne sait pas qui consomme, le consommateur n'a pas besoin d'etre disponible au
moment de la publication, et ajouter un nouvel abonne ne modifie pas le
producteur. L'alternative -- des appels HTTP directs -- cree un couplage fort.

!!! tip "Regle pratique"
    Un event interne peut changer librement (code prive). Un event externe
    est une **API publique** dont le schema doit rester retrocompatible.

---

## Redis Pub/Sub comme message broker

Redis offre un mecanisme Pub/Sub simple pour commencer. On cree une abstraction
pour la publication, suivant le meme pattern que notre `AbstractNotifications` :

```python
# adapters/redis_eventpublisher.py

import abc
import json
import redis


class AbstractEventPublisher(abc.ABC):
    """Interface abstraite pour la publication d'events externes."""

    @abc.abstractmethod
    def publish(self, channel: str, event_data: dict) -> None:
        raise NotImplementedError


class RedisEventPublisher(AbstractEventPublisher):
    """Publie des events sur des channels Redis Pub/Sub."""

    def __init__(self, host: str = "localhost", port: int = 6379):
        self.client = redis.Redis(host=host, port=port)

    def publish(self, channel: str, event_data: dict) -> None:
        self.client.publish(channel, json.dumps(event_data))
```

Notre handler `publish_allocated_event` utilise cet adapter via l'injection
de dependances du message bus :
```python
# service_layer/handlers.py

def publish_allocated_event(
    event: events.Allocated,
    publish: AbstractEventPublisher,
) -> None:
    """Publie un event d'allocation vers le message broker."""
    publish.publish(
        channel="line_allocated",
        event_data={
            "orderid": event.orderid,
            "sku": event.sku,
            "qty": event.qty,
            "batchref": event.batchref,
        },
    )
```

On injecte le publisher dans le bootstrap, comme pour les notifications :
```python
# service_layer/bootstrap.py

def bootstrap(
    # ... parametres existants ...
    publish: AbstractEventPublisher | None = None,
) -> MessageBus:
    if publish is None:
        publish = RedisEventPublisher()

    dependencies = {
        "notifications": notifications_adapter,
        "publish": publish,
        **extra_dependencies,
    }
    return MessageBus(uow=uow, event_handlers=EVENT_HANDLERS,
                      command_handlers=COMMAND_HANDLERS,
                      dependencies=dependencies)
```

---

## Consumer externe

Notre service doit aussi **recevoir** des events d'autres services. Quand un
systeme d'entrepot modifie la quantite d'un lot, il publie un event sur Redis.
Le consumer est un **processus separe** de l'API Flask qui ecoute Redis et
convertit les messages en commands internes :

```python
# entrypoints/redis_eventconsumer.py

import json
import redis
from allocation.domain import commands
from allocation.service_layer import bootstrap


def main():
    """Point d'entree du consumer Redis."""
    bus = bootstrap.bootstrap()
    client = redis.Redis("localhost", 6379)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("change_batch_quantity")

    for message in pubsub.listen():
        handle_message(message, bus)


def handle_message(message, bus):
    """Convertit un message Redis en command interne."""
    data = json.loads(message["data"])
    channel = message["channel"].decode()

    if channel == "change_batch_quantity":
        cmd = commands.ChangeBatchQuantity(
            ref=data["batchref"],
            qty=data["qty"],
        )
        bus.handle(cmd)
```

!!! note "Events externes deviennent des commands internes"
    Le consumer convertit les events externes en **commands**, pas en events.
    Du point de vue de notre service, le message entrant est une **intention**
    ("change cette quantite"), pas un fait passe. C'est une command qui peut
    echouer (lot introuvable, quantite invalide...).

### Le flux complet

```
Systeme externe                    Notre service
      │                                  │
      │  PUBLISH change_batch_quantity   │
      │  {"batchref":"b1", "qty":10}     │
      │ ────────────── Redis ──────────▶ │
      │                                  │
      │                     redis_eventconsumer.py
      │                          │
      │                     ChangeBatchQuantity(ref="b1", qty=10)
      │                          │
      │                     bus.handle(cmd) ──▶ handler ──▶ domaine
      │                          │
      │                     (si reallocation necessaire)
      │                     Deallocated ──▶ reallocate
      │                     Allocated  ──▶ publish vers Redis
```

---

## Le pattern Outbox

Que se passe-t-il si l'application crashe **apres** avoir commite en base,
mais **avant** d'avoir publie l'event sur Redis ? L'event est perdu. C'est
le probleme du **dual write**. Le pattern Outbox le resout :

1. **Dans la meme transaction**, on ecrit l'event dans une table `outbox`.
2. La transaction est commitee -- donnees **et** event persistes atomiquement.
3. Un **processus separe** (relay) lit l'outbox, publie vers le broker, puis
   marque les entrees comme publiees.

```
┌─────────────────────────────────────┐
│         Transaction BDD             │
│                                     │
│  UPDATE products SET ...            │
│  INSERT INTO outbox (type, data)    │
│  COMMIT  ◀── atomique              │
└──────────────────┬──────────────────┘
                   │ (processus separe)
┌──────────────────▼──────────────────┐
│           Outbox Relay              │
│                                     │
│  SELECT FROM outbox WHERE NOT pub.  │
│  redis.publish(channel, data)       │
│  UPDATE outbox SET published = TRUE │
└─────────────────────────────────────┘
```

```python
# adapters/orm.py (ajout de la table outbox)

outbox = Table(
    "outbox", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_type", String(100)),
    Column("data", Text),
    Column("created_at", DateTime, server_default=func.now()),
    Column("published", Boolean, default=False),
)
```

Le handler ecrit dans l'outbox au lieu de publier directement :
```python
def publish_allocated_event(event: events.Allocated,
                            uow: AbstractUnitOfWork) -> None:
    """Ecrit l'event dans la table outbox (meme transaction)."""
    with uow:
        uow.session.execute(outbox.insert().values(
            event_type="Allocated",
            data=json.dumps({
                "orderid": event.orderid, "sku": event.sku,
                "qty": event.qty, "batchref": event.batchref,
            }),
        ))
        uow.commit()
```

Le relay publie les events en attente :
```python
# entrypoints/outbox_relay.py

def main():
    engine = create_engine("sqlite:///allocation.db")
    r = redis.Redis("localhost", 6379)
    while True:
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT id, event_type, data FROM outbox "
                "WHERE published = FALSE ORDER BY id"
            )).fetchall()
            for row in rows:
                r.publish(row.event_type, row.data)
                conn.execute(text(
                    "UPDATE outbox SET published = TRUE WHERE id = :id"
                ), {"id": row.id})
        time.sleep(1)
```

!!! warning "Compromis"
    Le pattern Outbox garantit une publication **at-least-once** (au moins
    une fois). Le relay peut publier un event, crasher avant de le marquer
    comme publie, puis le republier. C'est pourquoi les consumers doivent
    etre **idempotents**.

---

## Idempotence des consumers

Dans un systeme distribue, les messages peuvent etre delivres **plus d'une
fois**. Nos consumers doivent etre **idempotents** : traiter le meme message
deux fois doit produire le meme resultat.

### Strategie 1 : operations naturellement idempotentes

"Fixer la quantite du lot B1 a 50" est idempotent -- l'executer deux fois
donne le meme resultat. "Ajouter 10 au lot B1" ne l'est pas.

C'est pourquoi notre command `ChangeBatchQuantity` prend une **quantite
absolue** (`qty=50`) plutot qu'un delta (`delta=+10`). Ce choix de design
rend le consumer naturellement idempotent.

### Strategie 2 : table de deduplication

Pour les operations non idempotentes, on enregistre les identifiants des
messages deja traites :
```python
processed_messages = Table(
    "processed_messages", metadata,
    Column("message_id", String(100), primary_key=True),
    Column("processed_at", DateTime, server_default=func.now()),
)

def handle_change_batch_quantity(message, bus):
    """Consumer idempotent avec deduplication."""
    data = json.loads(message["data"])
    message_id = data.get("message_id")

    if message_id and already_processed(message_id):
        return  # deja traite, on ignore

    cmd = commands.ChangeBatchQuantity(ref=data["batchref"], qty=data["qty"])
    bus.handle(cmd)

    if message_id:
        mark_as_processed(message_id)
```

!!! tip "Identifiants de messages"
    Pour que la deduplication fonctionne, chaque message doit porter un
    identifiant unique (`message_id`). Le producteur genere cet identifiant
    (typiquement un UUID) et l'inclut dans le payload.

---

## Resume : vue d'ensemble

```
┌────────────────────────────────────────────────┐
│           Service d'allocation                  │
│                                                │
│  Flask API ─┐   Consumer ─┐   Outbox Relay     │
│             ▼              ▼        │          │
│         ┌──────────────────────┐    │          │
│         │     Message Bus      │    │          │
│         └──────────┬───────────┘    │          │
│                    ▼                │          │
│         ┌──────────────────────┐    │          │
│         │  BDD (+ outbox)     ├────┘          │
│         └──────────────────────┘               │
└────────────────────┬───────────────────────────┘
                     │ Events (JSON)
           ┌─────────▼──────────┐
           │   Message Broker   │
           └──┬──────────────┬──┘
     ┌────────▼──┐    ┌──────▼────────┐
     │ Expedition│    │Systeme externe│
     └───────────┘    └───────────────┘
```

Trois points d'entree convergent vers le message bus : l'API Flask (requetes
HTTP), le consumer Redis (events externes), et l'outbox relay (publication).
Le domaine ne sait pas d'ou viennent les commands ni ou partent les events.

| Concept | Role |
|---------|------|
| **Event interne** | Circule dans le message bus en memoire |
| **Event externe** | Traverse les frontieres du service via un message broker |
| **Publisher** | Adapter qui serialise et publie les events vers le broker |
| **Consumer** | Processus qui ecoute le broker et cree des commands internes |
| **Pattern Outbox** | Garantit la publication fiable (ecriture BDD + relay) |
| **Idempotence** | Les consumers tolerent les messages dupliques |

### Principes cles

- Les events externes sont des **contrats** entre services -- leur schema
  doit etre stable et versionne.
- Le consumer convertit les events externes en **commands** internes,
  car c'est une intention du point de vue du service recepteur.
- Le pattern Outbox resout le probleme du **dual write** en garantissant
  l'atomicite entre la BDD et la publication.
- Concevez vos commands pour etre **naturellement idempotentes** quand
  c'est possible (quantites absolues plutot que deltas).
- Grace aux abstractions, les tests restent rapides et ne necessitent
  pas d'infrastructure externe.

!!! quote "A retenir"
    Les events externes transforment notre service en **bon citoyen** d'une
    architecture distribuee : il informe les autres de ce qui s'est passe
    chez lui, et reagit a ce qui se passe ailleurs, le tout sans couplage
    direct.
