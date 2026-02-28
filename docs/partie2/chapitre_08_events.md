# Chapitre 8 -- Events et le Message Bus

!!! info "Avant / Après"

    | | |
    |---|---|
    | **Avant** | Handler appelle `send_email()`, `publish()` directement |
    | **Après** | Domaine émet events, handlers réagissent via Message Bus |

> **Pattern** : Domain Events + Message Bus
> **Problème résolu** : Comment réagir aux changements du domaine sans coupler la logique métier aux effets de bord ?

---

## Le problème des effets de bord

Jusqu'ici, notre système d'allocation fait bien son travail : il choisit le bon lot,
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
def allouer(cmd, uow):
    ligne = LigneDeCommande(cmd.id_commande, cmd.sku, cmd.quantité)
    with uow:
        produit = uow.produits.get(sku=cmd.sku)
        réf_lot = produit.allouer(ligne)
        uow.commit()

    # Effets de bord empilés...
    send_email("client@example.com", f"Commande {cmd.id_commande} allouée")
    update_dashboard(cmd.sku, réf_lot)
    notify_warehouse(réf_lot, cmd.id_commande)
    publish_to_redis("allocation", {"id_commande": cmd.id_commande})
    return réf_lot
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
| `Alloué` | Une ligne de commande **a été** allouée à un lot |
| `Désalloué` | Une ligne de commande **a été** désallouée |
| `RuptureDeStock` | Le stock **est épuisé** pour un SKU donné |

Comparez avec les commands, qui sont des **demandes** au présent impératif :

| Command | Event correspondant |
|---------|---------------------|
| `Allouer` (alloue !) | `Alloué` (a été alloué) |
| `ModifierQuantitéLot` (change !) | `Désalloué` (a été désalloué) |

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
class Alloué(Event):
    """Une LigneDeCommande a été allouée à un Lot."""
    id_commande: str
    sku: str
    quantité: int
    réf_lot: str

@dataclass(frozen=True)
class Désalloué(Event):
    """Une LigneDeCommande a été désallouée d'un Lot."""
    id_commande: str
    sku: str
    quantité: int

@dataclass(frozen=True)
class RuptureDeStock(Event):
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
event = Alloué(id_commande="o1", sku="LAMP", quantité=10, réf_lot="batch-001")
event.quantité = 5  # FrozenInstanceError !
```

Chaque event porte exactement les **données nécessaires** pour que ses handlers puissent
travailler sans dépendre du contexte d'émission. `Alloué` porte l'`id_commande`, le `sku`,
la `quantité` et le `réf_lot`. `RuptureDeStock` ne porte que le `sku`. Un event est
**autosuffisant** : ses consommateurs n'ont pas besoin d'interroger la base de données.

---

## Les agrégats émettent des events

C'est le domaine qui sait quand quelque chose d'intéressant se produit. Pas le handler,
pas la couche service -- le **domaine lui-même**. C'est donc l'agrégat qui émet les events.

L'agrégat `Produit` maintient une liste d'events en attente :

```python
class Produit:
    def __init__(self, sku: str, lots=None, numéro_version: int = 0):
        self.sku = sku
        self.lots = lots or []
        self.numéro_version = numéro_version
        self.événements: list[events.Event] = []  # (1)
```

**(1)** La liste `self.événements` est le **tampon d'events**. Les events y sont accumulés
pendant l'exécution des méthodes métier, puis collectés par le Unit of Work.

### Émission lors de l'allocation

Quand l'allocation échoue par manque de stock, le `Produit` émet un `RuptureDeStock` :

```python
def allouer(self, ligne: LigneDeCommande) -> str:
    try:
        lot = next(
            l for l in sorted(self.lots) if l.peut_allouer(ligne)
        )
    except StopIteration:
        self.événements.append(events.RuptureDeStock(sku=ligne.sku))  # (1)
        return ""
    lot.allouer(ligne)
    self.numéro_version += 1
    return lot.référence
```

**(1)** Plutôt que de lever une exception, le domaine **enregistre un fait** : "le stock
est épuisé". C'est un changement de philosophie important.

### Émission lors du changement de quantité

Quand la quantité d'un lot diminue, chaque désallocation génère un `Désalloué` :

```python
def modifier_quantité_lot(self, réf: str, quantité: int) -> None:
    lot = next(l for l in self.lots if l.référence == réf)
    lot._quantité_achetée = quantité
    while lot.quantité_disponible < 0:
        ligne = lot.désallouer_une()
        self.événements.append(
            events.Désalloué(
                id_commande=ligne.id_commande, sku=ligne.sku, quantité=ligne.quantité,
            )
        )
```

Si trois lignes sont désallouées, il y aura trois events `Désalloué`, et chacun pourra
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
        for produit in self.produits.seen:
            while produit.événements:
                yield produit.événements.pop(0)
```

Le UoW itère sur tous les agrégats **vus** pendant la transaction, vide leur liste
d'events, et les transmet au bus. C'est le pont entre le domaine et l'infrastructure.

### Diagramme de séquence : émission et collecte des events

Le cycle complet, de la requête HTTP jusqu'au traitement des events :

```
Flask ──► MessageBus ──► handler(cmd) ──► Produit.allouer()
                                               │
                                               ▼
                                          événements.append(Alloué)
                                               │
               ◄── collect_new_events() ◄── uow.commit()
               │
               ▼
          handler(Alloué) ──► ajouter_allocation_vue()
```

**Point clé** : les events remontent du domaine via le UoW, puis sont re-dispatchés
par le bus. Le domaine n'appelle jamais directement un handler d'event — il se
contente d'accumuler des faits, et l'infrastructure les achemine.

---

## Les Event Handlers concrets

Voyons les handlers d'events définis dans `src/allocation/service_layer/handlers.py`.

### Notification de rupture de stock

```python
def envoyer_notification_rupture_stock(
    event: events.RuptureDeStock,
    notifications: AbstractNotifications,
) -> None:
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )
```

Ce handler reçoit un `RuptureDeStock` et une dépendance `notifications` injectée par le bus.
Pas besoin du UoW, pas besoin de la base -- juste l'event et l'adapter.

### Réallocation après désallocation

```python
def réallouer(
    event: events.Désalloué,
    uow: AbstractUnitOfWork,
) -> None:
    allouer(
        commands.Allouer(
            id_commande=event.id_commande, sku=event.sku, quantité=event.quantité,
        ),
        uow=uow,
    )
```

Quand une ligne est désallouée, ce handler crée une **nouvelle command** `Allouer` et
la traite. C'est une **chaîne réactive** : un changement de quantité entraîne des
désallocations, qui entraînent des réallocations.

### Le câblage dans le bootstrap

Les associations sont déclarées dans `src/allocation/service_layer/bootstrap.py` :

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
    events.RuptureDeStock: [handlers.envoyer_notification_rupture_stock],
}

COMMAND_HANDLERS: dict[type[commands.Command], Any] = {
    commands.CréerLot: handlers.ajouter_lot,
    commands.Allouer: handlers.allouer,
    commands.ModifierQuantitéLot: handlers.modifier_quantité_lot,
}
```

Pour ajouter un nouveau comportement, il suffit d'**ajouter un handler à la liste** --
sans modifier le code existant. C'est le principe Open/Closed en action :

```python
events.RuptureDeStock: [
    handlers.envoyer_notification_rupture_stock,  # email existant
    handlers.envoyer_sms_rupture_stock,            # SMS (nouveau !)
],
```

---

## Le flux complet

Déroulons un scénario de bout en bout : un changement de quantité de lot qui
déclenche une cascade d'events.

### Scénario : la quantité d'un lot diminue

Un fournisseur informe qu'un lot de 50 "BLUE-VASE" ne contiendra que 20 unités.
Trois commandes de 10 étaient allouées. Avec 20 unités, une doit être désallouée.

**Étape 1** -- La command entre dans le bus :

```python
cmd = ModifierQuantitéLot(réf="batch-001", quantité=20)
bus.handle(cmd)
```

**Étape 2** -- Le command handler charge le Produit et appelle la méthode métier.

**Étape 3** -- Le domaine désalloue une ligne et émet `Désalloué(id_commande="order-3",
sku="BLUE-VASE", quantité=10)`.

**Étape 4** -- Le UoW collecte l'event et l'ajoute à la queue du bus.

**Étape 5** -- Le bus dépile le `Désalloué` et appelle le handler `réallouer`.

**Étape 6** -- `réallouer` crée une command `Allouer` et la traite. Le système
tente de réallouer la ligne à un autre lot.

**Étape 7** -- Si la réallocation réussit, un `Alloué` est émis. S'il n'y a plus de
stock, un `RuptureDeStock` déclenche l'envoi d'une notification. La boucle continue
jusqu'à épuisement de la queue.

### Visualisation du flux

```
ModifierQuantitéLot (command)
    |
    v
modifier_quantité_lot (command handler)
    |
    v
Produit.modifier_quantité_lot() --- émet ---> Désalloué (event)
    |                                              |
    v                                              v
UoW.commit()                              réallouer (event handler)
                                                   |
                                                   v
                                           Allouer (command)
                                                   |
                                                   v
                                           Produit.allouer()
                                              /          \
                                        succès            échec
                                          |                |
                                          v                v
                                       Alloué         RuptureDeStock
                                          |                |
                                          v                v
                             publier_événement     envoyer_notification
```

---

## Résumé

Les Domain Events et le Message Bus résolvent le problème des effets de bord en
**découplant les faits de leurs conséquences**.

### Les concepts clés

| Concept | Rôle |
|---------|------|
| **Domain Event** | Objet immutable représentant un fait passé |
| **`self.événements`** | Tampon dans l'agrégat où les events sont accumulés |
| **Message Bus** | Dispatcher qui route events et commands vers leurs handlers |
| **`collect_new_events()`** | Méthode du UoW qui extrait les events des agrégats |
| **Event Handler** | Fonction qui réagit à un event |

### Les règles à retenir

1. **Les events sont au passé** : `Alloué`, pas `Allouer`. Ils constatent, ils ne
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
                    | (Produit.événements) |        |
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
                    | (email, réalloc, |
                    |  publish, ...)   |
                    +------------------+
```

## Exercices

!!! example "Exercice 1 -- Nouvel event"
    Créez un event `StockBas(sku, quantité_restante)` émis quand la quantité disponible d'un lot tombe sous un seuil (par exemple 10). Ajoutez un handler qui envoie une notification. Écrivez le test.

!!! example "Exercice 2 -- Boucle infinie"
    Que se passerait-il si un event handler émettait le même event qu'il traite ? Comment le message bus gère-t-il ce cas ? Proposez une protection.

!!! example "Exercice 3 -- Event vs exception"
    Dans le code actuel, `Produit.allouer()` émet un event `RuptureDeStock` au lieu de lever une exception. Quels sont les avantages de chaque approche ? Dans quels cas préférer l'exception ?

---

Avec les Domain Events et le Message Bus, notre architecture gagne en **extensibilité**
(ajouter des réactions sans modifier le domaine), en **testabilité** (chaque handler se
teste indépendamment) et en **clarté** (chaque composant a une responsabilité unique).

Dans le chapitre suivant, nous verrons comment le Message Bus peut devenir le **point
d'entrée principal** de toute l'application, en remplacement de la service layer
traditionnelle.
