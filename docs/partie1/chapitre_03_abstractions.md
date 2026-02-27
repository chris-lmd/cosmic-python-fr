# Chapitre 3 -- Couplage et abstractions

## Le probleme du couplage

Imaginez un systeme d'allocation de stock ou chaque composant connait directement
tous les autres. Le service layer appelle SQLAlchemy. Les handlers envoient des
emails via `smtplib`. Les tests doivent demarrer une base de donnees et un serveur
SMTP pour fonctionner.

Quand tout depend de tout, modifier un composant revient a tirer sur un fil :
tout le reste se detricote.

```
   Couplage direct : chaque module depend des details des autres.

   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
   │  Service      │────>│  SQLAlchemy   │────>│  PostgreSQL   │
   │  Layer        │     │  (ORM)        │     │  (BDD)        │
   └──────┬───────┘     └──────────────┘     └──────────────┘
          │
          │              ┌──────────────┐     ┌──────────────┐
          └─────────────>│  smtplib      │────>│  Serveur SMTP │
                         └──────────────┘     └──────────────┘

   Probleme : pour tester le Service Layer, il faut PostgreSQL ET un serveur SMTP.
   Pour changer de BDD, il faut modifier le Service Layer.
```

Ce schema illustre le **couplage direct** : les modules de haut niveau (la logique
d'orchestration) dependent des modules de bas niveau (la base de donnees, le
serveur de mail). Changer un detail d'infrastructure force a modifier le code metier.

Maintenant, comparons avec une architecture ou l'on a introduit des abstractions :

```
   Dependances inversees : tout pointe vers les abstractions.

   ┌──────────────┐     ┌────────────────────┐     ┌──────────────┐
   │  Service      │────>│  AbstractRepository │<────│ SqlAlchemy    │
   │  Layer        │     │  (port)             │     │ Repository    │
   └──────┬───────┘     └────────────────────┘     └──────────────┘
          │
          │              ┌────────────────────────┐  ┌──────────────┐
          └─────────────>│  AbstractNotifications  │<─│ Email         │
                         │  (port)                 │  │ Notifications │
                         └────────────────────────┘  └──────────────┘

   Le Service Layer ne connait QUE les abstractions.
   Les implementations concretes aussi.
   Personne ne depend des details.
```

Les fleches ont change de direction. Le Service Layer ne connait plus SQLAlchemy
ni `smtplib`. Il ne connait que des **abstractions**. C'est le coeur du
Dependency Inversion Principle.

---

## Le Dependency Inversion Principle (DIP)

Le DIP, cinquieme principe SOLID, s'enonce ainsi :

!!! note "Dependency Inversion Principle"
    **Les modules de haut niveau ne doivent pas dependre des modules de bas niveau.**
    Les deux doivent dependre d'abstractions.
    **Les abstractions ne doivent pas dependre des details.**
    Les details doivent dependre des abstractions.

En pratique, cela signifie que notre code metier ne doit jamais importer
`sqlalchemy` ou `smtplib`. Il travaille avec des **interfaces abstraites**,
et ce sont les couches d'infrastructure qui fournissent les implementations concretes.

### Illustration avec le Repository

Voici comment notre projet applique ce principe. D'abord, l'abstraction -- le
**port** -- qui definit le contrat :

```python
# src/allocation/adapters/repository.py

class AbstractRepository(abc.ABC):
    """
    Interface abstraite du repository.
    Definit le contrat que tout repository doit respecter.
    """

    def __init__(self) -> None:
        self.seen: set[model.Product] = set()

    def add(self, product: model.Product) -> None:
        """Ajoute un produit au repository et le marque comme vu."""
        self._add(product)
        self.seen.add(product)

    def get(self, sku: str) -> model.Product | None:
        """Recupere un produit par son SKU et le marque comme vu."""
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

Remarquez la structure : les methodes publiques `add` et `get` contiennent la
logique commune (le tracking via `self.seen`), tandis que les methodes prefixees
par `_` sont les points d'extension que chaque implementation concrete doit fournir.
C'est le **Template Method** pattern au service du DIP.

Ensuite, l'implementation concrete -- l'**adapter** -- qui sait parler a
SQLAlchemy :

```python
# src/allocation/adapters/repository.py

class SqlAlchemyRepository(AbstractRepository):
    """Implementation concrete du repository avec SQLAlchemy."""

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

Le Service Layer recoit un `AbstractRepository`. Il ne sait pas -- et **n'a pas
besoin de savoir** -- si derriere se cache PostgreSQL, un fichier CSV, ou un
simple dictionnaire en memoire.

### Illustration avec les notifications

Le meme pattern s'applique a d'autres preoccupations d'infrastructure.
Pour les notifications :

```python
# src/allocation/adapters/notifications.py

class AbstractNotifications(abc.ABC):
    """Interface abstraite pour les notifications."""

    @abc.abstractmethod
    def send(self, destination: str, message: str) -> None:
        raise NotImplementedError


class EmailNotifications(AbstractNotifications):
    """Implementation concrete envoyant des emails via SMTP."""

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

L'abstraction `AbstractNotifications` definit un contrat minimal : une seule
methode `send`. L'implementation `EmailNotifications` encapsule toute la
mecanique SMTP. Demain, si l'on veut envoyer des SMS ou des notifications Slack,
il suffit de creer un nouvel adapter sans toucher au code metier.

---

## Ports and Adapters (architecture hexagonale)

Le pattern que nous venons de voir porte un nom : **Ports and Adapters**,
aussi appele **architecture hexagonale** (Alistair Cockburn, 2005).

L'idee est simple :

- Le **domaine** est au centre. Il ne depend de rien d'exterieur.
- Les **ports** sont les interfaces que le domaine expose ou requiert
  (par exemple `AbstractRepository`, `AbstractNotifications`).
- Les **adapters** sont les implementations concretes qui connectent le domaine
  au monde exterieur (base de donnees, API, email, etc.).

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

Le domaine definit les **ports** : "j'ai besoin d'un mecanisme pour stocker et
recuperer des produits" et "j'ai besoin d'un mecanisme pour envoyer des
notifications". Ce sont des interfaces, pas des implementations. Les adapters
fournissent la realite concrete derriere ces interfaces.

L'avantage fondamental : on peut **remplacer n'importe quel adapter** sans
toucher au domaine ni a la logique d'orchestration.

---

## Quand abstraire, quand ne pas abstraire

L'abstraction est un outil puissant, mais elle a un cout : l'**indirection**.
Chaque couche d'abstraction ajoute un fichier, une interface, un niveau
supplementaire a comprendre pour le developpeur qui lit le code.

!!! warning "Le piege de l'abstraction prematuree"
    Abstraire trop tot, c'est construire un pont avant de savoir ou passe la
    riviere. On risque de creer des abstractions inutiles qui compliquent le
    code sans apporter de valeur.

### La regle des 3

Une heuristique utile est la **regle des 3** :

1. **3 implementations** : Si vous n'avez qu'une seule implementation (par
   exemple, un seul type de base de donnees), l'abstraction est peut-etre
   prematuree. Quand vous en avez 3 (SQL, fichier, in-memory pour les tests),
   le pattern devient evident.

2. **3 raisons de changer** : Si un composant pourrait changer pour 3 raisons
   differentes (changer de BDD, ameliorer les performances, supporter un
   nouveau format), c'est un bon candidat pour une abstraction.

Dans notre cas, le `Repository` a au moins deux implementations des le depart :

- `SqlAlchemyRepository` pour la production
- `FakeRepository` pour les tests

Et on pourrait facilement imaginer un `RedisRepository` pour du cache, ou un
`FileRepository` pour de l'export. L'abstraction se justifie pleinement.

### Quand NE PAS abstraire

Ne creez pas d'abstraction si :

- Il n'y a qu'une seule implementation et aucune raison previsible d'en avoir
  une deuxieme.
- Le code est si simple qu'une abstraction le rendrait **plus** difficile a lire.
- Vous le faites "au cas ou". Le **YAGNI** (You Ain't Gonna Need It) est
  un contrepoids sain au DIP.

Le bon reflexe : commencez concret, puis extrayez l'abstraction quand le
besoin se manifeste. Le refactoring est moins couteux qu'une mauvaise abstraction.

---

## Edge-to-edge testing avec des fakes

L'un des benefices les plus immediats de l'architecture Ports and Adapters
est la possibilite de faire du **edge-to-edge testing** : tester de bout en
bout sans infrastructure reelle, en remplacant les adapters par des **fakes**.

### Le FakeRepository

Voici comment on cree un fake pour le repository :

```python
# tests/unit/test_handlers.py

class FakeRepository(AbstractRepository):
    """
    Fake repository qui stocke les produits en memoire.
    Utilise pour les tests unitaires.
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

Le `FakeRepository` respecte exactement le meme contrat que le
`SqlAlchemyRepository`, mais il stocke tout dans un simple `set` Python.
Pas de base de donnees, pas de connexion, pas de migration. Les tests
s'executent en millisecondes.

### Le FakeNotifications

Meme principe pour les notifications :

```python
# tests/unit/test_handlers.py

class FakeNotifications(AbstractNotifications):
    """Fake pour capturer les notifications envoyees."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, destination: str, message: str) -> None:
        self.sent.append((destination, message))
```

Au lieu d'envoyer un vrai email, le fake stocke les appels dans une liste.
Dans les tests, on peut alors verifier :

```python
# Exemple d'assertion dans un test
assert notifications.sent == [
    ("stock@example.com", "Le SKU SMALL-TABLE est en rupture de stock")
]
```

### Pourquoi c'est puissant

Le edge-to-edge testing combine les avantages des tests unitaires et des
tests d'integration :

| Aspect                  | Tests unitaires | Tests d'integration | Edge-to-edge (fakes) |
|-------------------------|:---------------:|:-------------------:|:--------------------:|
| Vitesse                 | Rapide          | Lent                | Rapide               |
| Couverture de code      | Faible          | Elevee              | Elevee               |
| Fragilite               | Faible          | Elevee              | Faible               |
| Besoin d'infrastructure | Non             | Oui                 | Non                  |

Les tests avec fakes traversent toute la pile applicative -- du handler jusqu'au
repository -- mais sans jamais toucher a une vraie base de donnees. On teste
le **comportement reel** du systeme, pas un mock fragile qui simule un
scenario idealise.

---

## Resume

Ce chapitre a introduit les concepts de couplage et d'abstraction, et montre
comment le Dependency Inversion Principle et l'architecture Ports and Adapters
permettent de construire un systeme decouple et testable.

| Concept | Definition | Benefice |
|---------|-----------|----------|
| **Couplage** | Degre de dependance entre composants | Le reduire rend le systeme plus flexible |
| **DIP** | Dependre d'abstractions, pas de details concrets | Le code metier est isole de l'infrastructure |
| **Port** | Interface abstraite definissant un contrat (`AbstractRepository`) | Definit ce dont le domaine a besoin sans dire comment |
| **Adapter** | Implementation concrete d'un port (`SqlAlchemyRepository`) | Encapsule les details d'infrastructure |
| **Fake** | Implementation simple d'un port pour les tests (`FakeRepository`) | Tests rapides sans infrastructure |
| **Edge-to-edge testing** | Tester toute la pile avec des fakes | Couverture large, execution rapide |

!!! tip "A retenir"
    - Le couplage direct entre composants rend le systeme fragile et difficile a tester.
    - Le DIP inverse les dependances : tout le monde depend des abstractions.
    - L'architecture Ports and Adapters place le domaine au centre et l'infrastructure a la peripherie.
    - N'abstraire que quand c'est justifie : la regle des 3 est un bon guide.
    - Les fakes permettent un edge-to-edge testing rapide et fiable.

Dans le [chapitre suivant](chapitre_04_service_layer.md), nous verrons comment
la **Service Layer** orchestre les cas d'utilisation en s'appuyant sur ces
abstractions.
