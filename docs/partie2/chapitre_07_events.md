# Chapitre 7 -- Domain Events

> **Pattern** : Domain Events
> **Problème résolu** : Comment découpler les effets de bord (email, tableau de bord, notifications) de la logique métier ?

---

## Le problème : des effets de bord couplés aux handlers

Au chapitre 5, nous avons introduit la service layer avec des handlers fins qui orchestrent les opérations métier. Nos handlers `allouer` et `ajouter_lot` font bien leur travail : ils coordonnent le domaine et le Unit of Work.

Mais la réalité rattrape vite une architecture simple. Quand une allocation réussit, le système doit probablement :

- **Envoyer un email** de confirmation au client
- **Mettre à jour un tableau de bord** en temps réel
- **Notifier un service externe** (entrepôt, logistique, facturation)
- **Publier un événement** vers un bus de messages (Redis, Kafka...)

La tentation naturelle est d'empiler ces effets de bord dans le handler :

```python
# NE FAITES PAS ÇA -- handler monolithique
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

1. **Couplage** : le handler connaît les détails de chaque système externe. Si on ajoute un nouveau consommateur, il faut modifier le handler.
2. **Testabilité** : pour tester l'allocation, il faut mocker l'email, le dashboard, l'entrepôt et Redis. Les tests deviennent fragiles et lents.
3. **Responsabilité unique** : le handler orchestre la logique métier *et* gère les effets de bord. C'est une violation du Single Responsibility Principle.

La solution ? Séparer le **fait** (une allocation a eu lieu) de ses **conséquences** (envoyer un email, notifier un service). C'est exactement ce que permettent les **Domain Events**.

---

## Qu'est-ce qu'un Domain Event ?

Un **Domain Event** est un **fait immutable qui s'est produit dans le passé**. Ce n'est ni une demande, ni une intention : c'est un constat.

Les conventions de nommage reflètent cette nature :

| Exemple | Signification |
|---------|---------------|
| `Alloué` | Une ligne de commande **a été** allouée à un lot |
| `Désalloué` | Une ligne de commande **a été** désallouée d'un lot |
| `RuptureDeStock` | Le stock **est épuisé** pour un SKU donné |

Remarquez le **temps passé** (ou le constat d'un état) : quelque chose s'est déjà produit, on ne peut plus l'empêcher. Un event n'est pas une demande -- c'est une notification de ce qui est arrivé.

---

## La hiérarchie des events dans notre code

Notre module `src/allocation/domain/events.py` définit la hiérarchie suivante :

```python
from dataclasses import dataclass


class Event:
    """Classe de base pour tous les events du domaine."""
    pass


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

Les events sont déclarés avec `@dataclass(frozen=True)`, ce qui les rend **immutables**. Après création, aucun attribut ne peut être modifié :

```python
event = Alloué(id_commande="cmd-001", sku="CHAISE", quantité=10, réf_lot="lot-001")
event.quantité = 20  # ERREUR ! FrozenInstanceError
```

L'immutabilité est essentielle pour les events :

1. **Intégrité** : un fait passé ne change pas. Si une allocation a eu lieu pour 10 unités, ce fait reste vrai indépendamment de ce qui se passe ensuite.
2. **Sécurité** : plusieurs handlers peuvent réagir au même event sans risque qu'un handler modifie l'event pour les suivants.
3. **Traçabilité** : les events forment un journal fiable de ce qui s'est passé dans le système.

### La classe de base `Event`

La classe `Event` est un simple marqueur (marker class). Elle ne porte aucun comportement, mais elle permet de distinguer les events des autres types de messages dans le système. Nous verrons au chapitre 8 qu'il existe un autre type de message, les **Commands**, qui héritent d'une classe `Command` séparée.

---

## Les agrégats émettent les events

Au chapitre 2, nous avons vu que l'agrégat `Produit` est la frontière de cohérence : toutes les opérations d'allocation passent par lui. C'est aussi lui qui **émet les events**.

Rappelons la structure de `Produit` :

```python
class Produit:
    def __init__(self, sku, lots=None, numéro_version=0):
        self.sku = sku
        self.lots = lots or []
        self.numéro_version = numéro_version
        self.événements: list[events.Event] = []  # <-- la liste d'events
```

L'attribut `self.événements` est une simple liste Python. L'agrégat y ajoute des events au fur et à mesure de ses opérations. Il ne se préoccupe pas de **qui** va les traiter ni **comment** -- il se contente d'enregistrer les faits.

### Émission dans `allouer()`

```python
def allouer(self, ligne: LigneDeCommande) -> str:
    try:
        lot = next(
            l for l in sorted(self.lots)
            if l.peut_allouer(ligne)
        )
    except StopIteration:
        # Fait : le stock est épuisé
        self.événements.append(events.RuptureDeStock(sku=ligne.sku))
        return ""

    lot.allouer(ligne)
    self.numéro_version += 1
    # Fait : une allocation a eu lieu
    self.événements.append(
        events.Alloué(
            id_commande=ligne.id_commande,
            sku=ligne.sku,
            quantité=ligne.quantité,
            réf_lot=lot.référence,
        )
    )
    return lot.référence
```

Deux scénarios, deux events différents :

| Scénario | Event émis | Données |
|----------|-----------|---------|
| Un lot peut accueillir la ligne | `Alloué` | id_commande, sku, quantité, réf_lot |
| Aucun lot disponible | `RuptureDeStock` | sku |

### Émission dans `modifier_quantité_lot()`

```python
def modifier_quantité_lot(self, ref: str, quantité: int) -> None:
    lot = next(l for l in self.lots if l.référence == ref)
    lot._quantité_achetée = quantité
    while lot.quantité_disponible < 0:
        ligne = lot.désallouer_une()
        # Fait : une ligne a été désallouée
        self.événements.append(
            events.Désalloué(
                id_commande=ligne.id_commande,
                sku=ligne.sku,
                quantité=ligne.quantité,
            )
        )
```

Quand la quantité d'un lot est réduite en dessous des allocations existantes, chaque ligne en excédent est désallouée et un event `Désalloué` est émis. Cet event déclenchera plus tard une réallocation automatique.

---

## La récolte des events : `collect_new_events()`

Les events sont émis par les agrégats, mais qui les récupère ? C'est le **Unit of Work** (introduit au chapitre 4) qui s'en charge, grâce à la méthode `collect_new_events()` :

```python
class AbstractUnitOfWork(abc.ABC):

    def collect_new_events(self):
        """
        Collecte tous les événements émis par les agrégats vus
        pendant cette transaction.
        """
        if not self._committed:
            return
        for produit in self.produits.seen:
            while produit.événements:
                yield produit.événements.pop(0)
```

Le mécanisme est élégant :

1. **Le repository traque les agrégats** via l'attribut `seen` (chapitre 3). Chaque appel à `get()` ou `add()` ajoute le `Produit` à cet ensemble.
2. **Le UoW parcourt les agrégats vus** et vide leur liste d'événements un par un.
3. **Les events sont yieldés** via un générateur, ce qui permet un traitement paresseux.

### Le garde-fou : `_committed`

La première ligne de `collect_new_events()` est cruciale :

```python
if not self._committed:
    return
```

Si la transaction n'a pas été committée, la méthode **ne yield rien**. C'est un garde-fou essentiel :

```
Scénario SANS garde-fou :
    1. Le handler appelle produit.allouer(ligne)
    2. L'agrégat émet Alloué(...)
    3. Un bug provoque une exception AVANT le commit
    4. La transaction est rollbackée
    5. Mais l'event Alloué est quand même collecté et traité !
    6. => Un email "allocation confirmée" est envoyé pour une allocation qui n'existe pas
```

Le flag `_committed` (initialisé à `False` dans `__enter__`, mis à `True` dans `commit()`) empêche la propagation d'events fantômes -- des events qui correspondent à des opérations non persistées.

```
Scénario AVEC garde-fou :
    1. Le handler appelle produit.allouer(ligne)
    2. L'agrégat émet Alloué(...)
    3. Un bug provoque une exception AVANT le commit
    4. La transaction est rollbackée, _committed reste False
    5. collect_new_events() ne yield rien
    6. => Aucun email n'est envoyé. Cohérence préservée.
```

---

## Les handlers d'events : réagir aux faits

Maintenant que nous savons comment les events sont émis et collectés, voyons comment le système **réagit** à ces events. Chaque event peut avoir un ou plusieurs **handlers** -- des fonctions qui réagissent au fait passé.

### `envoyer_notification_rupture_stock`

Quand le stock est épuisé, il faut prévenir l'équipe :

```python
def envoyer_notification_rupture_stock(
    event: events.RuptureDeStock,
    notifications: AbstractNotifications,
) -> None:
    """Envoie une notification quand le stock est épuisé."""
    notifications.send(
        destination="stock@example.com",
        message=f"Rupture de stock pour le SKU {event.sku}",
    )
```

Ce handler utilise l'abstraction `AbstractNotifications` (chapitre 6) pour envoyer un email. En test, on injecte un `FakeNotifications` ; en production, c'est `EmailNotifications` qui parle en SMTP.

### `réallouer`

Quand une ligne est désallouée (par exemple suite à une réduction de quantité d'un lot), il faut la réallouer automatiquement :

```python
def réallouer(
    event: events.Désalloué,
    uow: AbstractUnitOfWork,
) -> None:
    """Réalloue automatiquement une ligne désallouée."""
    allouer(
        commands.Allouer(
            id_commande=event.id_commande,
            sku=event.sku,
            quantité=event.quantité,
        ),
        uow=uow,
    )
```

Ce handler est particulièrement intéressant : il transforme un event (`Désalloué`) en une nouvelle action (appeler le handler `allouer`). C'est un exemple de **cascade** : un event déclenche une action, qui peut elle-même émettre de nouveaux events.

### `publier_événement_allocation` et les handlers de vue

Deux autres handlers réagissent à l'event `Alloué` :

```python
def publier_événement_allocation(
    event: events.Alloué,
    uow: AbstractUnitOfWork,
) -> None:
    """Publie un événement d'allocation vers l'extérieur."""
    logger.info(
        "Allocation publiée : %s -> %s (quantité: %d)",
        event.id_commande, event.réf_lot, event.quantité,
    )


def ajouter_allocation_vue(
    event: events.Alloué,
    uow: AbstractUnitOfWork,
) -> None:
    """Met à jour le read model après une allocation."""
    with uow:
        uow.session.execute(
            text(
                "INSERT INTO allocations_view (id_commande, sku, réf_lot)"
                " VALUES (:id_commande, :sku, :réf_lot)"
            ),
            dict(id_commande=event.id_commande, sku=event.sku, réf_lot=event.réf_lot),
        )
        uow.commit()
```

Un même event, **plusieurs réactions indépendantes**. C'est la puissance des Domain Events : le handler `allouer` n'a aucune idée qu'un email est envoyé ou qu'un read model est mis à jour. Il se contente de faire l'allocation, et le domaine émet les faits.

---

## Le flux complet des events

Voici le flux complet, de l'émission à la réaction :

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  1. Le handler allouer() appelle produit.allouer(ligne)     │
│                                                             │
│  2. L'agrégat Produit émet events.Alloué(...)               │
│     dans self.événements                                    │
│                                                             │
│  3. Le handler appelle uow.commit()                         │
│     => _committed = True                                    │
│                                                             │
│  4. collect_new_events() parcourt produits.seen              │
│     => yield Alloué(...)                                    │
│                                                             │
│  5. Les event handlers réagissent :                         │
│     - publier_événement_allocation(event, uow)              │
│     - ajouter_allocation_vue(event, uow)                    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Et voici le diagramme de séquence pour le scénario de rupture de stock :

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌───────────────┐
│ Handler  │     │ Produit  │     │   UoW    │     │ Notifications │
└────┬─────┘     └────┬─────┘     └────┬─────┘     └──────┬────────┘
     │                │                │                   │
     │ allouer(ligne) │                │                   │
     │───────────────>│                │                   │
     │                │                │                   │
     │  "" (échec)    │                │                   │
     │<───────────────│                │                   │
     │                │ [self.événements += RuptureDeStock] │
     │                │                │                   │
     │ commit()       │                │                   │
     │───────────────────────────────>│                   │
     │                │  _committed=True                   │
     │                │                │                   │
     │ collect_new_events()            │                   │
     │───────────────────────────────>│                   │
     │    yield RuptureDeStock         │                   │
     │<───────────────────────────────│                   │
     │                │                │                   │
     │ envoyer_notification_rupture_stock(event)           │
     │────────────────────────────────────────────────────>│
     │                │                │     send(...)     │
     │                │                │                   │
```

---

## Récapitulatif du routage des events

Voici la table de routage qui associe chaque event à ses handlers :

| Event | Handler(s) | Effet |
|-------|-----------|-------|
| `Alloué` | `publier_événement_allocation` | Log l'allocation (placeholder pour Redis/Kafka) |
| `Alloué` | `ajouter_allocation_vue` | Met à jour le read model CQRS |
| `Désalloué` | `réallouer` | Crée une nouvelle commande d'allocation |
| `Désalloué` | `supprimer_allocation_vue` | Met à jour le read model CQRS |
| `RuptureDeStock` | `envoyer_notification_rupture_stock` | Envoie un email à l'équipe stock |

Nous verrons aux chapitres 8 et 9 comment ce routage est défini et comment un **Message Bus** orchestre la distribution des events vers les bons handlers.

---

## Résumé

| Concept | Ce qu'il faut retenir |
|---------|----------------------|
| **Domain Event** | Un fait immutable qui s'est produit dans le passé. Nommé au passé (Alloué, Désalloué). |
| **`frozen=True`** | Les events sont des dataclasses gelées : immutables après création. |
| **Émission** | Les agrégats émettent les events dans `self.événements`. Le domaine ne sait pas qui écoute. |
| **Récolte** | `collect_new_events()` dans le UoW parcourt les agrégats `seen` et vide leurs events. |
| **Garde-fou `_committed`** | Les events ne sont collectés que si la transaction a été committée. Pas de commit, pas d'event. |
| **Event handler** | Une fonction qui réagit à un event. Un event peut avoir 0, 1, ou N handlers. |
| **Découplage** | Le handler métier ne connaît pas ses conséquences. Les effets de bord sont des réactions aux events. |

---

## Exercices

!!! example "Exercice 1 -- Nouvel event"
    Définissez un nouvel event `LotCréé` émis par le handler `ajouter_lot` quand un nouveau lot est ajouté. Quels attributs porterait-il ? Quel handler pourrait y réagir ?

!!! example "Exercice 2 -- Events fantômes"
    Modifiez temporairement `collect_new_events()` pour supprimer le garde-fou `_committed`. Écrivez un test qui démontre le problème : un event est collecté alors que la transaction a échoué.

!!! example "Exercice 3 -- Compteur d'allocations"
    Créez un event handler `compter_allocations` qui réagit à `Alloué` en incrémentant un compteur en mémoire. Vérifiez que le handler est bien appelé après chaque allocation réussie (en utilisant les fakes du chapitre 6).

---

!!! quote "À retenir"
    Les Domain Events transforment une architecture monolithique en une architecture réactive. Au lieu de "quand j'alloue, j'envoie un email ET je mets à jour le dashboard ET je notifie l'entrepôt", on dit simplement "quand j'alloue, j'émets le fait Alloué". Les conséquences sont gérées ailleurs, par des handlers indépendants.

---

*Chapitre suivant : [Commands vs Events](chapitre_08_commands.md) -- la distinction entre une intention et un fait.*
