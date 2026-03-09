# Chapitre 8 -- Commands vs Events

> **Problème résolu** : Comment distinguer une intention (quelque chose à faire) d'un fait (quelque chose qui s'est passé) ?

---

## Deux types de messages

Au chapitre 7, nous avons découvert les **Domain Events** : des faits immutables émis par les agrégats. Mais dans notre système, il existe un autre type de message : les **Commands**.

La distinction est fondamentale :

| | **Command** | **Event** |
|---|------------|-----------|
| **Nature** | Une intention, une demande | Un fait, un constat |
| **Temps** | Impératif ("Fais ceci") | Passé ("Ceci s'est passé") |
| **Exemple** | `CréerLot`, `Allouer` | `Alloué`, `Désalloué` |
| **Émetteur** | L'extérieur (API, utilisateur) | Le domaine (agrégats) |
| **Handlers** | Exactement **un** | Zéro, un, ou **plusieurs** |
| **En cas d'erreur** | L'exception **remonte** à l'appelant | L'erreur est **loggée**, les autres handlers continuent |
| **Retour** | Peut retourner un résultat | Aucun retour |

---

## Les Commands : intentions du système

Une Command exprime ce que l'utilisateur ou un système externe **veut que le système fasse**. C'est une demande qui peut échouer.

Notre module `src/allocation/domain/commands.py` définit :

```python
from dataclasses import dataclass
from datetime import date
from typing import Optional


class Command:
    """Classe de base pour toutes les commands."""
    pass


@dataclass(frozen=True)
class CréerLot(Command):
    """Demande de création d'un nouveau lot de stock."""
    réf: str
    sku: str
    quantité: int
    eta: Optional[date] = None


@dataclass(frozen=True)
class Allouer(Command):
    """Demande d'allocation d'une ligne de commande."""
    id_commande: str
    sku: str
    quantité: int


@dataclass(frozen=True)
class ModifierQuantitéLot(Command):
    """Demande de modification de la quantité d'un lot."""
    réf: str
    quantité: int
```

### Conventions de nommage

Les Commands sont nommées à l'**impératif** :

| Command | Signification |
|---------|---------------|
| `CréerLot` | "Crée un lot" |
| `Allouer` | "Alloue une ligne" |
| `ModifierQuantitéLot` | "Modifie la quantité de ce lot" |

Comparez avec les Events, nommés au **passé** :

| Event | Signification |
|-------|---------------|
| `Alloué` | "Une allocation a été faite" |
| `Désalloué` | "Une désallocation a eu lieu" |
| `RuptureDeStock` | "Le stock est épuisé" |

Cette convention rend le code auto-documentant. En lisant le nom d'un message, on sait immédiatement s'il s'agit d'une intention ou d'un fait.

---

## Un handler par Command, N handlers par Event

C'est la différence architecturale la plus importante.

### Command : exactement un handler

Une Command est une demande précise, adressée à un handler précis. Il serait incohérent que deux handlers traitent la même commande `CréerLot` -- on aurait deux lots créés au lieu d'un.

```python
COMMAND_HANDLERS = {
    commands.CréerLot: handlers.ajouter_lot,          # un seul handler
    commands.Allouer: handlers.allouer,                # un seul handler
    commands.ModifierQuantitéLot: handlers.modifier_quantité_lot,  # un seul handler
}
```

Si le handler échoue, l'exception **remonte directement** à l'appelant. C'est attendu : si la création d'un lot échoue, l'API doit retourner une erreur 400 ou 500.

### Event : zéro à N handlers

Un event est un fait broadcast -- il ne s'adresse à personne en particulier. Plusieurs parties du système peuvent vouloir réagir au même fait :

```python
EVENT_HANDLERS = {
    events.Alloué: [
        handlers.publier_événement_allocation,    # handler 1
        handlers.ajouter_allocation_vue,          # handler 2
    ],
    events.Désalloué: [
        handlers.réallouer,                       # handler 1
        handlers.supprimer_allocation_vue,        # handler 2
    ],
    events.RuptureDeStock: [
        handlers.envoyer_notification_rupture_stock,  # handler unique
    ],
}
```

Si un event handler échoue, l'erreur est **loggée mais ne bloque pas** les autres handlers. C'est une décision délibérée : si l'envoi d'email échoue, on ne veut pas empêcher la mise à jour du read model.

!!! note "Où vivent ces dictionnaires ?"
    Ces dictionnaires de routage sont définis dans le Composition Root (`bootstrap.py`, [chapitre 9](chapitre_09_bootstrap_di.md)) et utilisés par le Message Bus ([chapitre 10](chapitre_10_message_bus.md)) pour distribuer chaque message au bon handler.

---

## Gestion des erreurs : tolérant vs strict

Ce schéma résume la stratégie :

```
Command (strict)                    Event (tolérant)
┌──────────────┐                    ┌──────────────┐
│   Allouer    │                    │   Alloué     │
└──────┬───────┘                    └──────┬───────┘
       │                                   │
       v                                   ├──────────────────┐
  ┌─────────┐                              v                  v
  │ allouer │                       ┌────────────┐    ┌────────────┐
  └────┬────┘                       │ handler 1  │    │ handler 2  │
       │                            └─────┬──────┘    └─────┬──────┘
       │ Erreur ?                         │ Erreur ?        │ OK
       │ => PROPAGE                       │ => LOG          │
       v                                  v                 v
  L'appelant                        Continue avec     Résultat
  reçoit l'exception               les autres        normal
```

### Pourquoi cette asymétrie ?

- Une **Command** représente un contrat : "je te demande de faire X". Si X échoue, c'est un problème que l'appelant doit savoir.
- Un **Event** représente un fait déjà accompli. Les réactions sont secondaires. Si l'envoi d'un email échoue, l'allocation a quand même eu lieu. On peut réessayer plus tard.

---

## Commands et Events sont tous deux immutables

Les Commands utilisent aussi `@dataclass(frozen=True)`. L'immutabilité est importante pour les deux :

```python
cmd = commands.Allouer(id_commande="cmd-001", sku="CHAISE", quantité=10)
cmd.quantité = 20  # ERREUR ! FrozenInstanceError
```

Pour une Command, l'immutabilité garantit que le message qui arrive au handler est identique à celui qui a été créé par l'appelant. Pas de modification accidentelle en cours de route.

---

## Quand utiliser Command vs Event ?

| Situation | Utilisez |
|-----------|---------|
| L'utilisateur envoie une requête HTTP | **Command** (CréerLot, Allouer) |
| Un système externe envoie un message | **Command** (ModifierQuantitéLot) |
| L'agrégat signale ce qui s'est passé | **Event** (Alloué, Désalloué) |
| Vous devez garantir le traitement | **Command** (erreur = exception) |
| Vous voulez découpler les réactions | **Event** (broadcast, tolérant) |

Une règle simple : **les Commands entrent dans le système, les Events sortent du domaine**.

```
Monde extérieur                     Domaine                    Réactions
                                    ┌──────────────────┐
  [HTTP POST]  ──Command──>         │                  │  ──Event──>  [Email]
                                    │     Produit      │
  [Message]    ──Command──>         │    (agrégat)     │  ──Event──>  [Read Model]
                                    │                  │
                                    └──────────────────┘  ──Event──>  [Redis]
```

---

## Résumé

| Concept | Ce qu'il faut retenir |
|---------|----------------------|
| **Command** | Une intention nommée à l'impératif. Exactement un handler. Les erreurs remontent. |
| **Event** | Un fait nommé au passé. Zéro à N handlers. Les erreurs sont loggées. |
| **Nommage** | Impératif pour les commands (`CréerLot`), passé pour les events (`Alloué`). |
| **Immutabilité** | Les deux utilisent `frozen=True`. Un message ne change pas après création. |
| **Flux** | Les commands entrent dans le système, les events sortent du domaine. |

---

## Exercices

!!! example "Exercice 1 -- Command ou Event ?"
    Classez les messages suivants : `AnnulerCommande`, `CommandeAnnulée`, `EnvoyerFacture`, `FactureEnvoyée`, `StockReçu`. Justifiez chaque choix.

!!! example "Exercice 2 -- Nouvelle Command"
    Définissez une Command `TransférerStock(réf_lot_source, réf_lot_destination, quantité)`. Quel event l'agrégat devrait-il émettre après le transfert ?

---

!!! quote "À retenir"
    La distinction Command/Event n'est pas qu'une convention de nommage. Elle détermine le nombre de handlers, la gestion des erreurs, et le sens du flux de données. Les Commands sont des ordres adressés au système ; les Events sont des faits constatés par le domaine.

---

*Chapitre suivant : [Le Message Bus](chapitre_10_message_bus.md) -- le cœur qui distribue Commands et Events aux bons handlers.*
