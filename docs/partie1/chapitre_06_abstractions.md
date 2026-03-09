# Chapitre 6 -- Couplage et abstractions

## Le problème du couplage

Dans les chapitres 3 à 5, nous avons introduit des abstractions -- `AbstractRepository`, `AbstractUnitOfWork` -- sans toujours formaliser le principe sous-jacent. Ce chapitre prend du recul : pourquoi abstraire, quand le faire, et quel cadre conceptuel utiliser.

Imaginez un système d'allocation de stock où chaque composant connaît directement
tous les autres. Le service layer appelle SQLAlchemy. Les handlers envoient des
emails via `smtplib`. Les tests doivent démarrer une base de données et un serveur
SMTP pour fonctionner.

Quand tout dépend de tout, modifier un composant revient à tirer sur un fil :
tout le reste se détricote.

```
   Couplage direct : chaque module dépend des détails des autres.

   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
   │  Service      │────>│  SQLAlchemy   │────>│  PostgreSQL   │
   │  Layer        │     │  (ORM)        │     │  (BDD)        │
   └──────┬───────┘     └──────────────┘     └──────────────┘
          │
          │              ┌──────────────┐     ┌──────────────┐
          └─────────────>│  smtplib      │────>│  Serveur SMTP │
                         └──────────────┘     └──────────────┘

   Problème : pour tester le Service Layer, il faut PostgreSQL ET un serveur SMTP.
   Pour changer de BDD, il faut modifier le Service Layer.
```

Ce schéma illustre le **couplage direct** : les modules de haut niveau (la logique
d'orchestration) dépendent des modules de bas niveau (la base de données, le
serveur de mail). Changer un détail d'infrastructure force à modifier le code métier.

Maintenant, comparons avec une architecture où l'on a introduit des abstractions :

```
   Dépendances inversées : tout pointe vers les abstractions.

   ┌──────────────┐     ┌────────────────────┐     ┌──────────────┐
   │  Service      │────>│  AbstractRepository │<────│ SqlAlchemy    │
   │  Layer        │     │  (port)             │     │ Repository    │
   └──────┬───────┘     └────────────────────┘     └──────────────┘
          │
          │              ┌────────────────────────┐  ┌──────────────┐
          └─────────────>│  AbstractNotifications  │<─│ Email         │
                         │  (port)                 │  │ Notifications │
                         └────────────────────────┘  └──────────────┘

   Le Service Layer ne connaît QUE les abstractions.
   Les implémentations concrètes aussi.
   Personne ne dépend des détails.
```

Les flèches ont changé de direction. Le Service Layer ne connaît plus SQLAlchemy
ni `smtplib`. Il ne connaît que des **abstractions**. C'est le coeur du
Dependency Inversion Principle.

---

## Le Dependency Inversion Principle (DIP)

Le DIP, cinquième principe SOLID, s'énonce ainsi :

!!! note "Dependency Inversion Principle"
    **Les modules de haut niveau ne doivent pas dépendre des modules de bas niveau.**
    Les deux doivent dépendre d'abstractions.
    **Les abstractions ne doivent pas dépendre des détails.**
    Les détails doivent dépendre des abstractions.

En pratique, cela signifie que notre code métier ne doit jamais importer
`sqlalchemy` ou `smtplib`. Il travaille avec des **interfaces abstraites**,
et ce sont les couches d'infrastructure qui fournissent les implémentations concrètes.

### Illustration avec le Repository

Voici comment notre projet applique ce principe. L'abstraction -- le **port** --
définit le contrat :

```python
# Rappel : l'interface définie au chapitre 3
class AbstractRepository(abc.ABC):
    def add(self, produit: model.Produit) -> None: ...
    def get(self, sku: str) -> model.Produit | None: ...
```

L'interface complète a été présentée au [chapitre 3](chapitre_03_repository.md).
L'essentiel est que le domaine dépend de cette **abstraction**, jamais de
l'implémentation concrète.

L'implémentation concrète `SqlAlchemyRepository` (détaillée au
[chapitre 3](chapitre_03_repository.md)) respecte ce contrat en déléguant à une
session SQLAlchemy. Le Service Layer reçoit un `AbstractRepository` : il ne sait
pas -- et **n'a pas besoin de savoir** -- si derrière se cache PostgreSQL, un
fichier CSV, ou un simple dictionnaire en mémoire.

### Illustration avec les notifications

Le même pattern s'applique à d'autres préoccupations d'infrastructure.
Pour les notifications :

```python
# src/allocation/adapters/notifications.py

class AbstractNotifications(abc.ABC):
    """Interface abstraite pour les notifications."""

    @abc.abstractmethod
    def send(self, destination: str, message: str) -> None:
        raise NotImplementedError


class EmailNotifications(AbstractNotifications):
    """Implémentation concrète envoyant des emails via SMTP."""

    def __init__(self, smtp_host: str = "localhost", smtp_port: int = 587):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port

    def send(self, destination: str, message: str) -> None:
        msg = f"Subject: Notification d'allocation\n\n{message}"
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
            smtp.sendmail(
                from_addr="allocations@example.com",
                to_addrs=[destination],
                msg=msg,
            )
```

L'abstraction `AbstractNotifications` définit un contrat minimal : une seule
méthode `send`. L'implémentation `EmailNotifications` encapsule toute la
mécanique SMTP. Demain, si l'on veut envoyer des SMS ou des notifications Slack,
il suffit de créer un nouvel adapter sans toucher au code métier.

---

## Ports and Adapters (architecture hexagonale)

Le pattern que nous venons de voir porte un nom : **Ports and Adapters**,
aussi appelé **architecture hexagonale** (Alistair Cockburn, 2005).

L'idée est simple :

- Le **domaine** est au centre. Il ne dépend de rien d'extérieur.
- Les **ports** sont les interfaces que le domaine expose ou requiert
  (par exemple `AbstractRepository`, `AbstractNotifications`).
- Les **adapters** sont les implémentations concrètes qui connectent le domaine
  au monde extérieur (base de données, API, email, etc.).

```
                        ┌─────────────────────────┐
                        │                         │
         ┌──────────┐   │   ┌─────────────────┐   │   ┌──────────────┐
         │ API Web  │───┼──>│                 │   │   │              │
         │ (adapter)│   │   │   Domaine        │   │<──│  PostgreSQL  │
         └──────────┘   │   │                 │   │   │  (adapter)   │
                        │   │   LigneDeCommande│   │   └──────────────┘
         ┌──────────┐   │   │   Lot            │   │
         │ CLI      │───┼──>│   Produit        │   │   ┌──────────────┐
         │ (adapter)│   │   │   allouer()      │   │<──│  SMTP        │
         └──────────┘   │   │                 │   │   │  (adapter)   │
                        │   └─────────────────┘   │   └──────────────┘
                        │         ports           │
                        └─────────────────────────┘
```

Dans notre projet, cela se traduit par :

| Concept          | Dans notre code                         |
|------------------|-----------------------------------------|
| Domaine          | `allocation.domain.model`               |
| Port (persistance) | `AbstractRepository`                 |
| Port (notifications) | `AbstractNotifications`           |
| Adapter (BDD)    | `SqlAlchemyRepository`                  |
| Adapter (email)  | `EmailNotifications`                    |

Le domaine définit les **ports** : "j'ai besoin d'un mécanisme pour stocker et
récupérer des produits" et "j'ai besoin d'un mécanisme pour envoyer des
notifications". Ce sont des interfaces, pas des implémentations. Les adapters
fournissent la réalité concrète derrière ces interfaces.

L'avantage fondamental : on peut **remplacer n'importe quel adapter** sans
toucher au domaine ni à la logique d'orchestration.

---

## Quand abstraire, quand ne pas abstraire

L'abstraction a un coût : l'**indirection**. Une heuristique utile est la
**règle des 3** : si vous avez (ou prévoyez) au moins 3 implémentations ou
3 raisons de changer un composant, l'abstraction se justifie. Dans notre cas,
le `Repository` a déjà `SqlAlchemyRepository`, `FakeRepository` et pourrait
accueillir un `RedisRepository` -- le pattern est évident.

À l'inverse, ne créez pas d'abstraction si une seule implémentation suffit,
si le code est si simple qu'une interface le rendrait plus obscur, ou si vous
le faites « au cas où ». Le **YAGNI** (You Ain't Gonna Need It) est un
contrepoids sain au DIP. Commencez concret, puis extrayez l'abstraction quand
le besoin se manifeste.

---

## Edge-to-edge testing avec des fakes

L'un des bénéfices les plus immédiats de l'architecture Ports and Adapters
est la possibilité de faire du **edge-to-edge testing** : tester de bout en
bout sans infrastructure réelle, en remplaçant les adapters par des **fakes**.

### Les Fakes

Le `FakeRepository` défini au [chapitre 3](chapitre_03_repository.md) respecte
le même contrat avec un simple `set` Python. Le même principe s'applique aux
notifications :

```python
class FakeNotifications(AbstractNotifications):
    def __init__(self):
        self.envoyées: list[tuple[str, str]] = []

    def send(self, destination: str, message: str) -> None:
        self.envoyées.append((destination, message))
```

Dans les tests, on peut alors vérifier :

```python
# Exemple d'assertion dans un test
assert notifications.envoyées == [
    ("stock@example.com", "Le SKU SMALL-TABLE est en rupture de stock")
]
```

### Pourquoi c'est puissant

Le edge-to-edge testing combine les avantages des tests unitaires et des
tests d'intégration :

| Aspect                  | Tests unitaires | Tests d'intégration | Edge-to-edge (fakes) |
|-------------------------|:---------------:|:-------------------:|:--------------------:|
| Vitesse                 | Rapide          | Lent                | Rapide               |
| Couverture de code      | Faible          | Élevée              | Élevée               |
| Fragilité               | Faible          | Élevée              | Faible               |
| Besoin d'infrastructure | Non             | Oui                 | Non                  |

Les tests avec fakes traversent toute la pile applicative -- du handler jusqu'au
repository -- mais sans jamais toucher à une vraie base de données. On teste
le **comportement réel** du système, pas un mock fragile qui simule un
scénario idéalisé.

---

## Exercices

!!! example "Exercice 1 -- Identifier les abstractions manquantes"
    Imaginez que votre système doit générer des fichiers PDF pour les bons de livraison. Dessinez le port (interface abstraite) et deux adapters (un réel avec une bibliothèque PDF, un fake pour les tests). Quels paramètres la méthode du port prend-elle ?

!!! example "Exercice 2 -- Mesurer le couplage"
    Listez tous les `import` de `handlers.py`. Combien pointent vers le domaine ? Combien vers l'infrastructure ? Si un import pointe vers l'infrastructure, est-ce un problème ? Pourquoi ?

!!! example "Exercice 3 -- YAGNI vs DIP"
    Votre application n'envoie des emails que via SMTP et n'aura jamais besoin d'autre chose. Faut-il quand même créer une `AbstractNotifications` ? Argumentez pour et contre.

---

## Résumé

Ce chapitre a introduit les concepts de couplage et d'abstraction, et montré
comment le Dependency Inversion Principle et l'architecture Ports and Adapters
permettent de construire un système découplé et testable.

| Concept | Définition | Bénéfice |
|---------|-----------|----------|
| **Couplage** | Degré de dépendance entre composants | Le réduire rend le système plus flexible |
| **DIP** | Dépendre d'abstractions, pas de détails concrets | Le code métier est isolé de l'infrastructure |
| **Port** | Interface abstraite définissant un contrat (`AbstractRepository`) | Définit ce dont le domaine a besoin sans dire comment |
| **Adapter** | Implémentation concrète d'un port (`SqlAlchemyRepository`) | Encapsule les détails d'infrastructure |
| **Fake** | Implémentation simple d'un port pour les tests (`FakeRepository`) | Tests rapides sans infrastructure |
| **Edge-to-edge testing** | Tester toute la pile avec des fakes | Couverture large, exécution rapide |

!!! tip "À retenir"
    - Le couplage direct entre composants rend le système fragile et difficile à tester.
    - Le DIP inverse les dépendances : tout le monde dépend des abstractions.
    - L'architecture Ports and Adapters place le domaine au centre et l'infrastructure à la périphérie.
    - N'abstraire que quand c'est justifié : la règle des 3 est un bon guide.
    - Les fakes permettent un edge-to-edge testing rapide et fiable.

---

*Prochain chapitre : [Domain Events](../partie2/chapitre_07_events.md) -- comment les événements émis par les agrégats déclenchent des actions dans le reste du système.*
