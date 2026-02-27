# Chapitre 3 -- Couplage et abstractions

## Le problème du couplage

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
ni `smtplib`. Il ne connaît que des **abstractions**. C'est le cœur du
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

Voici comment notre projet applique ce principe. D'abord, l'abstraction -- le
**port** -- qui définit le contrat :

```python
# src/allocation/adapters/repository.py

class AbstractRepository(abc.ABC):
    """
    Interface abstraite du repository.
    Définit le contrat que tout repository doit respecter.
    """

    def __init__(self) -> None:
        self.seen: set[model.Product] = set()

    def add(self, product: model.Product) -> None:
        """Ajoute un produit au repository et le marque comme vu."""
        self._add(product)
        self.seen.add(product)

    def get(self, sku: str) -> model.Product | None:
        """Récupère un produit par son SKU et le marque comme vu."""
        product = self._get(sku)
        if product:
            self.seen.add(product)
        return product

    @abc.abstractmethod
    def _add(self, product: model.Product) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def _get(self, sku: str) -> model.Product | None:
        raise NotImplementedError
```

Remarquez la structure : les méthodes publiques `add` et `get` contiennent la
logique commune (le tracking via `self.seen`), tandis que les méthodes préfixées
par `_` sont les points d'extension que chaque implémentation concrète doit fournir.
C'est le **Template Method** pattern au service du DIP.

Ensuite, l'implémentation concrète -- l'**adapter** -- qui sait parler à
SQLAlchemy :

```python
# src/allocation/adapters/repository.py

class SqlAlchemyRepository(AbstractRepository):
    """Implémentation concrète du repository avec SQLAlchemy."""

    def __init__(self, session: Session):
        super().__init__()
        self.session = session

    def _add(self, product: model.Product) -> None:
        self.session.add(product)

    def _get(self, sku: str) -> model.Product | None:
        return (
            self.session.query(model.Product)
            .filter_by(sku=sku)
            .first()
        )
```

Le Service Layer reçoit un `AbstractRepository`. Il ne sait pas -- et **n'a pas
besoin de savoir** -- si derrière se cache PostgreSQL, un fichier CSV, ou un
simple dictionnaire en mémoire.

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
                        │   │   OrderLine      │   │   └──────────────┘
         ┌──────────┐   │   │   Batch          │   │
         │ CLI      │───┼──>│   Product        │   │   ┌──────────────┐
         │ (adapter)│   │   │   allocate()     │   │<──│  SMTP        │
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

L'abstraction est un outil puissant, mais elle a un coût : l'**indirection**.
Chaque couche d'abstraction ajoute un fichier, une interface, un niveau
supplémentaire à comprendre pour le développeur qui lit le code.

!!! warning "Le piège de l'abstraction prématurée"
    Abstraire trop tôt, c'est construire un pont avant de savoir où passe la
    rivière. On risque de créer des abstractions inutiles qui compliquent le
    code sans apporter de valeur.

### La règle des 3

Une heuristique utile est la **règle des 3** :

1. **3 implémentations** : Si vous n'avez qu'une seule implémentation (par
   exemple, un seul type de base de données), l'abstraction est peut-être
   prématurée. Quand vous en avez 3 (SQL, fichier, in-memory pour les tests),
   le pattern devient évident.

2. **3 raisons de changer** : Si un composant pourrait changer pour 3 raisons
   différentes (changer de BDD, améliorer les performances, supporter un
   nouveau format), c'est un bon candidat pour une abstraction.

Dans notre cas, le `Repository` a au moins deux implémentations dès le départ :

- `SqlAlchemyRepository` pour la production
- `FakeRepository` pour les tests

Et on pourrait facilement imaginer un `RedisRepository` pour du cache, ou un
`FileRepository` pour de l'export. L'abstraction se justifie pleinement.

### Quand NE PAS abstraire

Ne créez pas d'abstraction si :

- Il n'y a qu'une seule implémentation et aucune raison prévisible d'en avoir
  une deuxième.
- Le code est si simple qu'une abstraction le rendrait **plus** difficile à lire.
- Vous le faites "au cas où". Le **YAGNI** (You Ain't Gonna Need It) est
  un contrepoids sain au DIP.

Le bon réflexe : commencez concret, puis extrayez l'abstraction quand le
besoin se manifeste. Le refactoring est moins coûteux qu'une mauvaise abstraction.

---

## Edge-to-edge testing avec des fakes

L'un des bénéfices les plus immédiats de l'architecture Ports and Adapters
est la possibilité de faire du **edge-to-edge testing** : tester de bout en
bout sans infrastructure réelle, en remplaçant les adapters par des **fakes**.

### Le FakeRepository

Voici comment on crée un fake pour le repository :

```python
# tests/unit/test_handlers.py

class FakeRepository(AbstractRepository):
    """
    Fake repository qui stocke les produits en mémoire.
    Utilisé pour les tests unitaires.
    """

    def __init__(self, products: list[model.Product] | None = None):
        super().__init__()
        self._products = set(products or [])

    def _add(self, product: model.Product) -> None:
        self._products.add(product)

    def _get(self, sku: str) -> model.Product | None:
        return next((p for p in self._products if p.sku == sku), None)

    def _get_by_batchref(self, batchref: str) -> model.Product | None:
        return next(
            (p for p in self._products
             for b in p.batches if b.reference == batchref),
            None,
        )
```

Le `FakeRepository` respecte exactement le même contrat que le
`SqlAlchemyRepository`, mais il stocke tout dans un simple `set` Python.
Pas de base de données, pas de connexion, pas de migration. Les tests
s'exécutent en millisecondes.

### Le FakeNotifications

Même principe pour les notifications :

```python
# tests/unit/test_handlers.py

class FakeNotifications(AbstractNotifications):
    """Fake pour capturer les notifications envoyées."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, destination: str, message: str) -> None:
        self.sent.append((destination, message))
```

Au lieu d'envoyer un vrai email, le fake stocke les appels dans une liste.
Dans les tests, on peut alors vérifier :

```python
# Exemple d'assertion dans un test
assert notifications.sent == [
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

Dans le [chapitre suivant](chapitre_04_service_layer.md), nous verrons comment
la **Service Layer** orchestre les cas d'utilisation en s'appuyant sur ces
abstractions.
