# Chapitre 11 -- Events externes et communication entre services

!!! info "Avant / Après"

    | | |
    |---|---|
    | **Avant** | Service isolé, communications HTTP directes |
    | **Après** | Events via Redis Pub/Sub, Consumer convertit en commands |

!!! abstract "Ce que vous allez apprendre"
    - La différence entre events internes et events externes
    - Comment publier des events vers un message broker (Redis Pub/Sub)
    - Comment consommer des events externes et les convertir en commands internes
    - Le pattern Outbox pour garantir la publication fiable des events
    - L'importance de l'idempotence dans les consumers

---

## Events internes vs events externes

Jusqu'ici, nos events circulent uniquement **à l'intérieur** de notre application.
Quand un `Produit` émet un event `Alloué`, le message bus le dispatch vers
`publier_événement_allocation` ou `réallouer` -- tout cela dans le même processus.
Mais d'autres services ont besoin de savoir qu'une allocation a eu lieu, et des
systèmes externes doivent pouvoir nous informer de changements. Comment faire
communiquer ces services sans les coupler ?

| Aspect | Event interne | Event externe |
|--------|--------------|---------------|
| **Périmètre** | Au sein d'un seul processus | Entre plusieurs services |
| **Transport** | Message bus en mémoire | Message broker (Redis, RabbitMQ, Kafka...) |
| **Format** | Objets Python (dataclasses) | Données sérialisées (JSON, Protobuf...) |
| **Fiabilité** | Garantie par le processus | Nécessite des mécanismes dédiés |
| **Couplage** | Même bounded context | Entre bounded contexts différents |

Un event **interne** comme `Désalloué` déclenche une réallocation dans le même
service -- c'est un détail d'implémentation invisible de l'extérieur. Un event
**externe** comme `Alloué` informe les autres services qu'une allocation a
eu lieu -- c'est un **contrat** dont d'autres équipes dépendent.

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
            │ Expédition │   │ Facturation   │
            └────────────┘   └───────────────┘
```

Les events externes offrent un **découplage temporel et spatial** : le producteur
ne sait pas qui consomme, le consommateur n'a pas besoin d'être disponible au
moment de la publication, et ajouter un nouvel abonné ne modifie pas le
producteur. L'alternative -- des appels HTTP directs -- crée un couplage fort.

!!! tip "Règle pratique"
    Un event interne peut changer librement (code privé). Un event externe
    est une **API publique** dont le schéma doit rester rétrocompatible.

---

## Redis Pub/Sub comme message broker

Redis offre un mécanisme Pub/Sub simple pour commencer. On crée une abstraction
pour la publication, suivant le même pattern que notre `AbstractNotifications` :

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

Notre handler `publier_événement_allocation` utilise cet adapter via l'injection
de dépendances du message bus :
```python
# service_layer/handlers.py

def publier_événement_allocation(
    event: events.Alloué,
    publish: AbstractEventPublisher,
) -> None:
    """Publie un event d'allocation vers le message broker."""
    publish.publish(
        channel="line_allocated",
        event_data={
            "id_commande": event.id_commande,
            "sku": event.sku,
            "quantité": event.quantité,
            "réf_lot": event.réf_lot,
        },
    )
```

On injecte le publisher dans le bootstrap, comme pour les notifications :
```python
# service_layer/bootstrap.py

def bootstrap(
    # ... paramètres existants ...
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
système d'entrepôt modifie la quantité d'un lot, il publie un event sur Redis.
Le consumer est un **processus séparé** de l'API Flask qui écoute Redis et
convertit les messages en commands internes :

```python
# entrypoints/redis_eventconsumer.py

import json
import redis
from allocation.domain import commands
from allocation.service_layer import bootstrap


def main():
    """Point d'entrée du consumer Redis."""
    bus = bootstrap.bootstrap()
    client = redis.Redis("localhost", 6379)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("modifier_quantité_lot")

    for message in pubsub.listen():
        handle_message(message, bus)


def handle_message(message, bus):
    """Convertit un message Redis en command interne."""
    data = json.loads(message["data"])
    channel = message["channel"].decode()

    if channel == "modifier_quantité_lot":
        cmd = commands.ModifierQuantitéLot(
            réf=data["réf_lot"],
            quantité=data["quantité"],
        )
        bus.handle(cmd)
```

!!! note "Events externes deviennent des commands internes"
    Le consumer convertit les events externes en **commands**, pas en events.
    Du point de vue de notre service, le message entrant est une **intention**
    ("change cette quantité"), pas un fait passé. C'est une command qui peut
    échouer (lot introuvable, quantité invalide...).

### Le flux complet

```
Système externe                    Notre service
      │                                  │
      │  PUBLISH modifier_quantité_lot  │
      │  {"réf_lot":"b1", "quantité":10}│
      │ ────────────── Redis ──────────▶ │
      │                                  │
      │                     redis_eventconsumer.py
      │                          │
      │                     ModifierQuantitéLot(réf="b1", quantité=10)
      │                          │
      │                     bus.handle(cmd) ──▶ handler ──▶ domaine
      │                          │
      │                     (si réallocation nécessaire)
      │                     Désalloué ──▶ réallouer
      │                     Alloué   ──▶ publier vers Redis
```

---

## Le pattern Outbox

Que se passe-t-il si l'application crashe **après** avoir commité en base,
mais **avant** d'avoir publié l'event sur Redis ? L'event est perdu. C'est
le problème du **dual write**. Le pattern Outbox le résout :

1. **Dans la même transaction**, on écrit l'event dans une table `outbox`.
2. La transaction est commitée -- données **et** event persistés atomiquement.
3. Un **processus séparé** (relay) lit l'outbox, publie vers le broker, puis
   marque les entrées comme publiées.

```
┌─────────────────────────────────────┐
│         Transaction BDD             │
│                                     │
│  UPDATE produits SET ...            │
│  INSERT INTO outbox (type, data)    │
│  COMMIT  ◀── atomique              │
└──────────────────┬──────────────────┘
                   │ (processus séparé)
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

Le handler écrit dans l'outbox au lieu de publier directement :
```python
def publier_événement_allocation(event: events.Alloué,
                            uow: AbstractUnitOfWork) -> None:
    """Écrit l'event dans la table outbox (même transaction)."""
    with uow:
        uow.session.execute(outbox.insert().values(
            event_type="Alloué",
            data=json.dumps({
                "id_commande": event.id_commande, "sku": event.sku,
                "quantité": event.quantité, "réf_lot": event.réf_lot,
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
    comme publié, puis le republier. C'est pourquoi les consumers doivent
    être **idempotents**.

---

## Idempotence des consumers

Dans un système distribué, les messages peuvent être délivrés **plus d'une
fois**. Nos consumers doivent être **idempotents** : traiter le même message
deux fois doit produire le même résultat.

### Stratégie 1 : opérations naturellement idempotentes

"Fixer la quantité du lot B1 à 50" est idempotent -- l'exécuter deux fois
donne le même résultat. "Ajouter 10 au lot B1" ne l'est pas.

C'est pourquoi notre command `ModifierQuantitéLot` prend une **quantité
absolue** (`quantité=50`) plutôt qu'un delta (`delta=+10`). Ce choix de design
rend le consumer naturellement idempotent.

### Stratégie 2 : table de déduplication

Pour les opérations non idempotentes, on enregistre les identifiants des
messages déjà traités :
```python
processed_messages = Table(
    "processed_messages", metadata,
    Column("message_id", String(100), primary_key=True),
    Column("processed_at", DateTime, server_default=func.now()),
)

def handle_modifier_quantité_lot(message, bus):
    """Consumer idempotent avec déduplication."""
    data = json.loads(message["data"])
    message_id = data.get("message_id")

    if message_id and already_processed(message_id):
        return  # déjà traité, on ignore

    cmd = commands.ModifierQuantitéLot(réf=data["réf_lot"], quantité=data["quantité"])
    bus.handle(cmd)

    if message_id:
        mark_as_processed(message_id)
```

!!! tip "Identifiants de messages"
    Pour que la déduplication fonctionne, chaque message doit porter un
    identifiant unique (`message_id`). Le producteur génère cet identifiant
    (typiquement un UUID) et l'inclut dans le payload.

---

## Résumé : vue d'ensemble

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
     │ Expédition│    │Système externe│
     └───────────┘    └───────────────┘
```

Trois points d'entrée convergent vers le message bus : l'API Flask (requêtes
HTTP), le consumer Redis (events externes), et l'outbox relay (publication).
Le domaine ne sait pas d'où viennent les commands ni où partent les events.

| Concept | Rôle |
|---------|------|
| **Event interne** | Circule dans le message bus en mémoire |
| **Event externe** | Traverse les frontières du service via un message broker |
| **Publisher** | Adapter qui sérialise et publie les events vers le broker |
| **Consumer** | Processus qui écoute le broker et crée des commands internes |
| **Pattern Outbox** | Garantit la publication fiable (écriture BDD + relay) |
| **Idempotence** | Les consumers tolèrent les messages dupliqués |

### Principes clés

- Les events externes sont des **contrats** entre services -- leur schéma
  doit être stable et versionné.
- Le consumer convertit les events externes en **commands** internes,
  car c'est une intention du point de vue du service récepteur.
- Le pattern Outbox résout le problème du **dual write** en garantissant
  l'atomicité entre la BDD et la publication.
- Concevez vos commands pour être **naturellement idempotentes** quand
  c'est possible (quantités absolues plutôt que deltas).
- Grâce aux abstractions, les tests restent rapides et ne nécessitent
  pas d'infrastructure externe.

## Exercices

!!! example "Exercice 1 -- FakeEventPublisher"
    Implémentez un `FakeEventPublisher` qui stocke les events publiés dans une liste. Écrivez un test qui vérifie que quand une allocation réussit, un event est publié sur le bon channel avec les bonnes données.

!!! example "Exercice 2 -- Consumer robuste"
    Le consumer actuel ne gère pas les erreurs de parsing JSON. Ajoutez un `try/except` et un logging. Que devrait-il faire si le message est invalide : l'ignorer, le republier, ou le mettre dans une dead letter queue ?

!!! example "Exercice 3 -- Schéma d'events"
    Les events externes sont des contrats entre services. Proposez un mécanisme pour versionner les events (ex: `Alloué_v1`, `Alloué_v2`). Comment gérer la rétrocompatibilité ?

---

!!! quote "À retenir"
    Les events externes transforment notre service en **bon citoyen** d'une
    architecture distribuée : il informe les autres de ce qui s'est passé
    chez lui, et réagit à ce qui se passe ailleurs, le tout sans couplage
    direct.
