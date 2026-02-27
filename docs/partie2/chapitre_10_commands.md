# Chapitre 10 -- Commands et Events : distinguer les intentions des faits

Jusqu'ici, notre message bus traite des messages. Mais tous les messages ne se
valent pas. Quand l'API envoie une demande d'allocation, c'est une **instruction
explicite** : "alloue cette ligne de commande". Quand le domaine signale qu'un
produit est en rupture de stock, c'est un **constat** : "le stock est épuisé".

Cette distinction n'est pas cosmétique. Elle a des conséquences concrètes sur la
manière dont le système traite ces messages, sur la gestion des erreurs, et sur
le couplage entre les composants.

Dans ce chapitre, nous allons formaliser cette distinction en introduisant deux
types de messages : les **commands** et les **events**.

---

## La distinction fondamentale

La différence tient en une phrase :

> **Une command est une intention** -- quelque chose qui *doit* être fait.
> **Un event est un fait** -- quelque chose qui *s'est produit*.

Prenons un exemple concret dans notre domaine d'allocation de stock :

- `Allocate` est une command : "je veux que cette ligne soit allouée". C'est une
  demande adressée au système. Elle peut réussir ou échouer, et l'appelant veut
  savoir lequel des deux s'est produit.

- `Allocated` est un event : "cette ligne a été allouée au lot batch-001". C'est
  un fait accompli. On ne peut pas "refuser" un fait. On peut seulement y
  réagir.

Pensez-y comme la différence entre dire *"Réserve-moi une table pour 20h"*
(command) et *"La table 12 a été réservée pour 20h"* (event). La première est
une demande qui peut être déclinée. La seconde est une information que le
restaurant diffuse à qui veut l'entendre -- le serveur, le chef, le système de
réservation en ligne.

---

## Les classes Command

Les commands vivent dans leur propre module, séparé des events. Voici notre
fichier `commands.py` :

```python title="src/allocation/domain/commands.py"
"""
Commands du domaine.

Les commands représentent des intentions : quelque chose que
le système doit faire. Contrairement aux events (faits passés),
les commands sont des demandes qui peuvent échouer.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional


class Command:
    """Classe de base pour toutes les commands."""
    pass


@dataclass(frozen=True)
class CreateBatch(Command):
    """Demande de création d'un nouveau lot de stock."""

    ref: str
    sku: str
    qty: int
    eta: Optional[date] = None


@dataclass(frozen=True)
class Allocate(Command):
    """Demande d'allocation d'une ligne de commande."""

    orderid: str
    sku: str
    qty: int


@dataclass(frozen=True)
class ChangeBatchQuantity(Command):
    """Demande de modification de la quantité d'un lot."""

    ref: str
    qty: int
```

Et voici les events correspondants dans `events.py` :

```python title="src/allocation/domain/events.py"
"""
Events du domaine.

Les events représentent des faits qui se sont produits dans le système.
Ils sont immuables et nommés au passé (quelque chose s'est passé).
"""

from dataclasses import dataclass


class Event:
    """Classe de base pour tous les events du domaine."""
    pass


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

### Pourquoi deux fichiers séparés ?

Trois raisons motivent cette séparation :

1. **Clarté sémantique.** Un développeur qui ouvre `commands.py` sait
   immédiatement qu'il regarde les actions que le système sait exécuter. Celui
   qui ouvre `events.py` voit les choses qui peuvent se produire dans le
   domaine. Ce sont deux catalogues distincts.

2. **Conventions de nommage différentes.** Les commands sont nommées à
   l'**impératif** (`CreateBatch`, `Allocate`, `ChangeBatchQuantity`) : ce sont
   des ordres. Les events sont nommés au **passé composé** (`Allocated`,
   `Deallocated`, `OutOfStock`) : ce sont des constats.

3. **Cycle de vie différent.** Les commands viennent de l'extérieur du domaine
   (API, CLI, autre service). Les events sont émis par le domaine lui-même. Les
   séparer reflète cette différence d'origine.

!!! note "frozen=True"
    Les deux types utilisent `frozen=True`. Un message -- qu'il soit command ou
    event -- est un **objet immuable**. On ne modifie pas une intention après
    l'avoir formulée, et on ne réécrit pas l'histoire.

---

## Caractéristiques des Commands

Les commands ont quatre propriétés distinctives :

### 1. Nommées à l'impératif

Le nom d'une command exprime ce que l'on veut que le système fasse :

| Command                | Signification                         |
|------------------------|---------------------------------------|
| `CreateBatch`          | "Crée un nouveau lot"                 |
| `Allocate`             | "Alloue cette ligne de commande"      |
| `ChangeBatchQuantity`  | "Modifie la quantité de ce lot"       |

On parle au système comme on parlerait à un collègue : *"Fais ceci."*

### 2. Exactement un handler

Chaque command est prise en charge par **un seul handler**. C'est logique : si
quelqu'un vous demande de faire quelque chose, il y a un responsable pour
exécuter cette demande, pas zéro, pas trois.

```python title="bootstrap.py -- enregistrement des command handlers"
COMMAND_HANDLERS: dict[type[commands.Command], Any] = {
    commands.CreateBatch: handlers.add_batch,
    commands.Allocate: handlers.allocate,
    commands.ChangeBatchQuantity: handlers.change_batch_quantity,
}
```

Remarquez le type : `dict[type[commands.Command], Callable]` -- une seule
fonction par command, pas une liste.

Si aucun handler n'est enregistré pour une command, c'est une erreur de
configuration. Le message bus lèvera une `ValueError` :

```python
handler = self.command_handlers.get(type(command))
if handler is None:
    raise ValueError(f"Aucun handler pour la command {type(command)}")
```

### 3. Les erreurs remontent

Quand un command handler échoue, l'exception **remonte jusqu'à l'appelant**.
C'est le comportement attendu : si vous demandez au système d'allouer une ligne
et que le SKU n'existe pas, vous voulez le savoir immédiatement.

```python title="handlers.py -- un handler qui peut lever une exception"
def allocate(
    cmd: commands.Allocate,
    uow: AbstractUnitOfWork,
) -> str:
    line = model.OrderLine(orderid=cmd.orderid, sku=cmd.sku, qty=cmd.qty)
    with uow:
        product = uow.products.get(sku=cmd.sku)
        if product is None:
            raise InvalidSku(f"SKU inconnu : {cmd.sku}")
        batchref = product.allocate(line)
        uow.commit()
    return batchref
```

L'API Flask peut alors attraper cette exception et retourner un code HTTP
adapté :

```python title="flask_app.py -- l'API traduit l'erreur en réponse HTTP"
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
```

### 4. Dirigées vers un destinataire précis

Une command a un **destinataire clair**. `Allocate` est destinée au handler
`allocate`. Il n'y a pas d'ambiguité, pas de broadcast. C'est une communication
point-à-point.

---

## Caractéristiques des Events

Les events présentent des propriétés symétriquement opposées :

### 1. Nommés au passé

Un event décrit quelque chose qui s'est *déjà* produit :

| Event          | Signification                            |
|----------------|------------------------------------------|
| `Allocated`    | "Une ligne a été allouée"                |
| `Deallocated`  | "Une ligne a été désallouée"             |
| `OutOfStock`   | "Le stock est épuisé"                    |

On ne dit pas *"Désalloue"* (ce serait une command), on dit *"Ça a été
désalloué"*.

### 2. Zéro, un ou N handlers

Un event peut intéresser **plusieurs parties** du système, ou aucune. C'est du
broadcast : l'émetteur ne sait pas (et ne devrait pas savoir) qui écoute.

```python title="bootstrap.py -- enregistrement des event handlers"
EVENT_HANDLERS: dict[type[events.Event], list] = {
    events.Allocated: [handlers.publish_allocated_event],
    events.Deallocated: [handlers.reallocate],
    events.OutOfStock: [handlers.send_out_of_stock_notification],
}
```

Remarquez le type : `dict[type[events.Event], list[Callable]]` -- une **liste**
de handlers par event. Demain, si l'on veut aussi envoyer un SMS en cas de
rupture de stock, il suffit d'ajouter un handler à la liste de `OutOfStock` :

```python
events.OutOfStock: [
    handlers.send_out_of_stock_notification,
    handlers.send_sms_to_warehouse_manager,  # nouveau handler
],
```

L'émetteur de l'event `OutOfStock` n'a pas besoin d'être modifié. C'est
l'**Open/Closed Principle** en action.

### 3. Les erreurs sont capturées

Quand un event handler échoue, l'erreur est **loguée mais pas propagée**. Les
autres handlers continuent de s'exécuter. C'est fondamental : un fait s'est
produit, et toutes les parties intéressées doivent avoir la chance d'en être
informées, même si l'une d'elles rencontre un problème.

Si l'envoi du SMS échoue, ça ne doit pas empêcher la notification par email.

### 4. Broadcast

L'émetteur d'un event ne choisit pas ses destinataires. Il se contente de dire
*"Voilà ce qui s'est passé"* et le message bus se charge de distribuer
l'information. N'importe quel composant peut s'abonner.

---

## Le dispatch différencié dans le MessageBus

La distinction command/event se matérialise dans deux méthodes séparées du
message bus. Comparons-les :

```python title="src/allocation/service_layer/messagebus.py"
class MessageBus:

    def handle(self, message: Message) -> list[Any]:
        """Point d'entrée principal."""
        self.queue = [message]
        results: list[Any] = []
        while self.queue:
            message = self.queue.pop(0)
            if isinstance(message, events.Event):
                self._handle_event(message)          # (1)
            elif isinstance(message, commands.Command):
                result = self._handle_command(message)  # (2)
                results.append(result)
            else:
                raise ValueError(f"Message de type inconnu : {type(message)}")
        return results
```

1. Les events sont délégués à `_handle_event`.
2. Les commands sont déléguées à `_handle_command`, et leur **résultat** est
   collecté.

### `_handle_command` : strict et direct

```python
def _handle_command(self, command: commands.Command) -> Any:
    """Dispatch une command vers son unique handler."""
    logger.debug("Traitement de la command %s", command)
    handler = self.command_handlers.get(type(command))
    if handler is None:
        raise ValueError(f"Aucun handler pour la command {type(command)}")
    result = self._call_handler(handler, command)
    self.queue.extend(self.uow.collect_new_events())
    return result
```

Points clés :

- **Un seul handler** est recherché (pas une liste).
- Si le handler est absent, une `ValueError` est levée.
- Les exceptions du handler **ne sont pas attrapées** : elles remontent
  naturellement à l'appelant.
- Le résultat du handler est **retourné** (utile pour `allocate` qui retourne
  le `batchref`).

### `_handle_event` : tolérant et exhaustif

```python
def _handle_event(self, event: events.Event) -> None:
    """Dispatch un event vers tous ses handlers."""
    for handler in self.event_handlers.get(type(event), []):
        try:
            logger.debug("Traitement de l'event %s avec %s", event, handler)
            self._call_handler(handler, event)
            self.queue.extend(self.uow.collect_new_events())
        except Exception:
            logger.exception("Erreur lors du traitement de l'event %s", event)
```

Points clés :

- **Tous les handlers** sont exécutés (boucle `for`).
- Si aucun handler n'est enregistré, `get(..., [])` retourne une liste vide :
  aucune erreur, l'event est simplement ignoré.
- Chaque handler est enveloppé dans un `try/except`. Si l'un échoue, les
  **autres continuent**.
- Les erreurs sont **logguées** via `logger.exception`, pas propagées.
- La méthode ne retourne **rien** (`None`).

### Le contraste résumé en code

Le tableau suivant met en parallèle les deux approches :

```
_handle_command                    _handle_event
─────────────────────────────────  ─────────────────────────────────
UN handler par command             N handlers par event
handler absent = ValueError        handler absent = rien ne se passe
exception = propagée               exception = logguée, on continue
retourne un résultat               ne retourne rien
```

---

## De l'API au domaine : le parcours d'un message

Pour bien comprendre comment les deux types de messages coopèrent, suivons le
parcours d'un changement de quantité de lot.

**Étape 1 -- L'API reçoit une requête HTTP et crée une command.**

```python
cmd = commands.ChangeBatchQuantity(ref="batch-001", qty=5)
bus.handle(cmd)
```

C'est une **command** : quelqu'un demande au système de modifier une quantité.
Si le lot n'existe pas, on veut une erreur.

**Étape 2 -- Le command handler s'exécute.**

```python
def change_batch_quantity(cmd, uow):
    with uow:
        product = uow.products.get_by_batchref(batchref=cmd.ref)
        product.change_batch_quantity(ref=cmd.ref, qty=cmd.qty)
        uow.commit()
```

Le modèle de domaine ajuste la quantité. Si des lignes doivent être désallouées,
il émet un event `Deallocated` sur l'agrégat.

**Étape 3 -- Le message bus collecte les events et les traite.**

L'event `Deallocated(orderid="o1", sku="SMALL-TABLE", qty=10)` est ajouté à la
queue. Le bus le dispatche vers son handler :

```python
def reallocate(event: events.Deallocated, uow):
    allocate(
        commands.Allocate(
            orderid=event.orderid,
            sku=event.sku,
            qty=event.qty,
        ),
        uow=uow,
    )
```

Notez que le handler d'event **crée une command** (`Allocate`) pour réallouer.
C'est un pattern courant : un event déclenche une action, et cette action est
formulée comme une command.

**Étape 4 -- L'allocation réussit ou émet un `OutOfStock`.**

Si le stock est insuffisant, le domaine émet `OutOfStock(sku="SMALL-TABLE")`,
ce qui déclenche l'envoi d'une notification. Si un handler de notification
échoue, l'erreur est logguée mais ne fait pas échouer la chaîne.

```
ChangeBatchQuantity (command)
    └── change_batch_quantity handler
            └── Deallocated (event)
                    └── reallocate handler
                            └── Allocated (event) ... ou OutOfStock (event)
                                                          └── send_notification
```

---

## Quand créer une Command vs un Event ?

La règle est simple :

!!! tip "La règle d'or"
    **Demande extérieure = Command.** Un utilisateur, une API, un fichier CSV,
    un message d'un autre service *demande* au système de faire quelque chose.

    **Réaction interne = Event.** Le domaine *constate* que quelque chose s'est
    produit et en informe le reste du système.

Quelques exemples pour illustrer :

| Situation                                           | Type    | Pourquoi                                                |
|-----------------------------------------------------|---------|---------------------------------------------------------|
| L'API reçoit `POST /allocate`                       | Command | Demande explicite d'un acteur extérieur                 |
| Un fichier CSV contient de nouveaux lots             | Command | Le fichier *demande* la création des lots               |
| Le domaine constate qu'une ligne a été allouée       | Event   | Fait interne, broadcast à qui veut l'entendre           |
| La quantité d'un lot diminue et des lignes débordent | Event   | Le domaine constate la désallocation                    |
| Un autre service demande de modifier un lot          | Command | Demande explicite, même si elle vient d'un service      |
| Le stock tombe à zéro                               | Event   | Constat, les intéressés réagissent comme ils l'entendent |

### Cas particulier : les réactions en chaîne

Comme on l'a vu dans le parcours ci-dessus, un event handler peut lui-même
émettre des commands ou des events. Le handler `reallocate` réagit à un event
`Deallocated` en créant une command `Allocate`. C'est parfaitement normal :

- L'event `Deallocated` est un **fait** : "cette ligne a été désallouée".
- La command `Allocate` est une **intention** : "réalloue cette ligne".

Le fait déclenche l'intention. L'intention peut réussir ou échouer. Si elle
échoue dans un handler d'event, l'erreur est logguée.

---

## Résumé

### Tableau comparatif

| Aspect              | Command                          | Event                               |
|---------------------|----------------------------------|--------------------------------------|
| **Sémantique**      | Intention (quelque chose à faire) | Fait (quelque chose s'est produit)   |
| **Nommage**         | Impératif : `Allocate`           | Passé : `Allocated`                  |
| **Nombre de handlers** | Exactement 1                  | 0, 1 ou N                           |
| **Erreur du handler** | Propagée à l'appelant           | Logguée, les autres continuent       |
| **Résultat**        | Peut retourner une valeur        | Pas de valeur de retour              |
| **Origine**         | Extérieure (API, CLI, service)   | Interne (domaine, handler)           |
| **Communication**   | Point-à-point                    | Broadcast                            |
| **Handler absent**  | `ValueError`                     | Silencieux (liste vide)              |

### Ce que nous avons appris

- Les **commands** et les **events** sont les deux types de messages qui
  circulent dans notre message bus. Les distinguer n'est pas un luxe
  académique : cela détermine la gestion des erreurs, le couplage entre
  composants, et l'extensibilité du système.

- Les commands sont des **demandes explicites**, nommées à l'impératif, avec un
  handler unique qui peut échouer bruyamment. Elles viennent de l'extérieur du
  domaine.

- Les events sont des **faits constatés**, nommés au passé, avec zéro ou
  plusieurs handlers qui échouent silencieusement. Ils sont émis par le domaine.

- Le message bus implémente cette distinction dans deux méthodes :
  `_handle_command` (strict) et `_handle_event` (tolérant).

- La **règle pratique** : si c'est une demande venue de l'extérieur, c'est une
  command. Si c'est une réaction interne au domaine, c'est un event.

### Structure des fichiers

```
src/allocation/
├── domain/
│   ├── commands.py       # Les intentions : CreateBatch, Allocate, ...
│   ├── events.py         # Les faits : Allocated, Deallocated, OutOfStock
│   └── model.py          # Le modèle de domaine qui émet les events
├── service_layer/
│   ├── bootstrap.py      # Enregistrement des handlers (command et event)
│   ├── handlers.py       # Les fonctions qui traitent commands et events
│   └── messagebus.py     # Le dispatch différencié (_handle_command / _handle_event)
└── entrypoints/
    └── flask_app.py      # L'API qui crée des commands
```
