# Chapitre 12 -- CQRS (Command Query Responsibility Segregation)

## Le probleme de la lecture

Dans les chapitres precedents, nous avons construit un modele de domaine riche :
des aggregats (`Product`), des entites (`Batch`), des value objects (`OrderLine`),
des invariants metier, un repository pour la persistance et un message bus pour
l'orchestration. Tout cela forme un chemin d'ecriture solide et bien protege.

Mais posons-nous une question simple : que se passe-t-il quand un utilisateur
veut simplement **afficher** les allocations d'une commande ?

Avec notre architecture actuelle, le chemin ressemblerait a ceci :

```
   Requete GET /allocations/order-123
        │
        v
   Repository.get(sku=...)          # Charge un Product entier
        │
        v
   Product                           # Avec tous ses Batch
     ├── Batch("batch-001")          # Chaque Batch avec ses allocations
     │     └── {OrderLine, OrderLine, ...}
     ├── Batch("batch-002")
     │     └── {OrderLine, OrderLine, ...}
     └── Batch("batch-003")
           └── {OrderLine, ...}
        │
        v
   Parcours de toutes les allocations pour trouver celles de "order-123"
        │
        v
   Serialisation en JSON
```

Pour repondre a une question triviale -- "quels SKUs sont alloues a cette
commande ?" -- on charge un aggregat complet avec tous ses lots, toutes ses
allocations, on reconstruit le graphe d'objets, on traverse les relations...
C'est comme sortir toute la bibliotheque pour trouver un seul livre.

Le modele de domaine est optimise pour **proteger les invariants en ecriture** :

- L'aggregat `Product` garantit qu'on n'alloue pas plus que le stock disponible.
- Le version number protege contre les acces concurrents.
- Les events tracent chaque changement d'etat.

Mais pour la **lecture**, on n'a besoin d'aucune de ces garanties. Pas
d'invariants a verifier, pas de concurrence a gerer, pas d'events a emettre.
On veut juste des donnees, le plus vite possible.

!!! note "Le constat fondamental"
    Les besoins de lecture et d'ecriture sont **fondamentalement differents**.
    Utiliser le meme modele pour les deux, c'est faire un compromis qui penalise
    les deux cotes.

---

## Le principe CQRS

CQRS -- Command Query Responsibility Segregation -- propose une solution
radicale : **separer completement les chemins d'ecriture et de lecture**.

L'idee vient du principe CQS (Command Query Separation) de Bertrand Meyer,
applique a l'echelle de l'architecture :

- Les **commands** (ecriture) passent par le domaine, le message bus, le
  repository. Elles modifient l'etat du systeme.
- Les **queries** (lecture) interrogent directement la base de donnees.
  Elles ne modifient rien.

```
   CQRS : deux chemins distincts pour deux besoins distincts.

   ┌──────────────────────────────────────────────────────────────────┐
   │                          API Flask                               │
   │                                                                  │
   │   POST /allocate              GET /allocations/<orderid>         │
   │        │                              │                          │
   └────────┼──────────────────────────────┼──────────────────────────┘
            │                              │
            v                              v
   ┌─────────────────┐           ┌─────────────────┐
   │   WRITE PATH    │           │   READ PATH     │
   │                 │           │                 │
   │   Command       │           │   View          │
   │     │           │           │     │           │
   │     v           │           │     v           │
   │   Message Bus   │           │   SQL direct    │
   │     │           │           │     │           │
   │     v           │           │     v           │
   │   Handler       │           │   allocations_  │
   │     │           │           │   view (table)  │
   │     v           │           │                 │
   │   Domain Model  │           └─────────────────┘
   │     │           │
   │     v           │
   │   Repository    │
   │     │           │
   │     v           │
   │   ORM / BDD     │
   │                 │
   └─────────────────┘
```

A gauche, le chemin d'ecriture traverse toute la pile : validation, logique
metier, persistance via le repository, emission d'events. A droite, le chemin
de lecture va droit au but : une fonction, une requete SQL, un resultat.

La cle de CQRS, c'est qu'on utilise **deux modeles differents** pour deux
besoins differents :

| Aspect              | Write model                         | Read model                       |
|---------------------|-------------------------------------|----------------------------------|
| **Objectif**        | Proteger les invariants metier      | Servir des donnees rapidement    |
| **Structure**       | Aggregats, entites, value objects   | Tables denormalisees, vues       |
| **Acces**           | Via repository + domain model       | Via SQL direct                   |
| **Complexite**      | Riche (logique metier)              | Simple (projection de donnees)   |
| **Optimise pour**   | Coherence et regles metier          | Performance de lecture            |

---

## Le read model : `allocations_view`

Le read model est une table **denormalisee** concue specifiquement pour
repondre a une question de lecture. Contrairement aux tables du write model
(qui sont normalisees avec des cles etrangeres et des jointures), le read
model contient exactement les colonnes dont la vue a besoin, dans un format
directement exploitable.

Dans notre ORM, la table `allocations_view` est definie ainsi :

```python
# src/allocation/adapters/orm.py

allocations_view = Table(
    "allocations_view",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("orderid", String(255)),
    Column("sku", String(255)),
    Column("batchref", String(255)),
)
```

Comparons avec les tables du write model, qui necessiteraient des jointures
pour obtenir la meme information :

```
   WRITE MODEL (normalise)               READ MODEL (denormalise)
   ─────────────────────────             ─────────────────────────

   order_lines                            allocations_view
   ┌────────┬──────┬─────┐               ┌─────────┬──────┬──────────┐
   │orderid │ sku  │ qty │               │ orderid │ sku  │ batchref │
   ├────────┼──────┼─────┤               ├─────────┼──────┼──────────┤
   │order-1 │LAMP  │  10 │               │ order-1 │LAMP  │ batch-01 │
   └────┬───┴──────┴─────┘               │ order-1 │TABLE │ batch-03 │
        │                                 │ order-2 │LAMP  │ batch-02 │
        │  allocations                    └─────────┴──────┴──────────┘
        │  ┌────────────┬──────────┐
        └──│orderline_id│ batch_id │      Pas de jointure. Pas de
           ├────────────┼──────────┤      reconstruction d'objet.
           │     1      │    3     │      Tout est deja pret a lire.
           └────────────┴──────────┘
                             │
        batches              │
        ┌────┬──────────┬────┘
        │ id │reference │ ...
        ├────┼──────────┤
        │  3 │batch-01  │
        └────┴──────────┘

   Pour obtenir "quels SKUs sont alloues a order-1", le write model
   exige 3 tables et 2 jointures. Le read model : 1 table, 0 jointure.
```

Le read model duplique de l'information -- oui, c'est volontaire. En
denormalisant, on echange de l'espace disque (bon marche) contre de la vitesse
de lecture (precieuse). C'est un compromis classique et parfaitement justifie
pour les chemins de lecture.

---

## Les views : des fonctions de lecture pure

Le cote query de CQRS est implemente par des **views** : des fonctions simples
qui executent des requetes SQL directes sur le read model. Pas de domaine, pas
de repository, pas d'aggregat. Juste une requete et un resultat.

```python
# src/allocation/views/views.py

"""
Views (lecture) pour le pattern CQRS.

Les views sont des fonctions de lecture pure qui interrogent
directement la base de données, sans passer par le modèle de domaine.

C'est le côté Query de CQRS : on sépare les chemins d'écriture
(qui passent par le domaine et le message bus) des chemins de
lecture (qui interrogent directement la BDD pour la performance).
"""

from allocation.service_layer import unit_of_work


def allocations(orderid: str, uow: unit_of_work.SqlAlchemyUnitOfWork) -> list[dict]:
    """
    Retourne les allocations pour un orderid donné.

    Requête SQL directe sur la table de lecture (read model).
    """
    with uow:
        results = uow.session.execute(
            "SELECT sku, batchref FROM allocations_view WHERE orderid = :orderid",
            dict(orderid=orderid),
        )
        return [dict(r._mapping) for r in results]
```

Remarquez a quel point c'est simple. La fonction `allocations` :

1. Ouvre une session via le unit of work.
2. Execute une requete SQL brute sur `allocations_view`.
3. Retourne une liste de dictionnaires.

Pas de `Product`, pas de `Batch`, pas de `OrderLine`. Pas de reconstruction
d'aggregat, pas de traversee de relations. La requete va directement chercher
les donnees la ou elles sont, dans le format exact dont l'API a besoin.

Le endpoint Flask qui utilise cette view est tout aussi direct :

```python
# src/allocation/entrypoints/flask_app.py

@app.route("/allocations/<orderid>", methods=["GET"])
def allocations_view_endpoint(orderid: str):
    """
    GET /allocations/<orderid>

    Retourne les allocations pour une commande donnée (lecture CQRS).
    """
    from allocation.views import views

    result = views.allocations(orderid, bus.uow)
    if not result:
        return "not found", 404
    return jsonify(result), 200
```

Le contraste avec les endpoints d'ecriture est frappant :

| Endpoint d'ecriture (`POST /allocate`)          | Endpoint de lecture (`GET /allocations`) |
|--------------------------------------------------|------------------------------------------|
| Construit une `Command`                          | Appelle une view directement             |
| Envoie au message bus                            | Pas de message bus                       |
| Le handler charge un aggregat via le repository  | La view fait un `SELECT` SQL             |
| Le domaine verifie les invariants                | Aucune verification metier               |
| Des events sont emis                             | Aucun event                              |
| Le resultat est un effet de bord (allocation)    | Le resultat est une projection de donnees |

---

## Mise a jour du read model par les event handlers

Si le read model est une table separee, comment reste-t-il synchronise avec le
write model ? Par les **event handlers**. Quand une allocation est effectuee, le
domaine emet un event `Allocated`. Un handler ecoute cet event et met a jour la
table `allocations_view`.

Voici comment l'event `Allocated` est defini :

```python
# src/allocation/domain/events.py

@dataclass(frozen=True)
class Allocated(Event):
    """Un OrderLine a été alloué à un Batch."""

    orderid: str
    sku: str
    qty: int
    batchref: str
```

L'event contient toutes les informations necessaires pour mettre a jour le read
model : le `orderid`, le `sku` et le `batchref`. C'est exactement ce que la
table `allocations_view` attend.

Le handler de mise a jour du read model ressemblerait a ceci :

```python
# src/allocation/service_layer/handlers.py

def add_allocation_to_read_model(
    event: events.Allocated,
    uow: AbstractUnitOfWork,
) -> None:
    """Met à jour le read model quand une allocation est effectuée."""
    with uow:
        uow.session.execute(
            "INSERT INTO allocations_view (orderid, sku, batchref)"
            " VALUES (:orderid, :sku, :batchref)",
            dict(orderid=event.orderid, sku=event.sku, batchref=event.batchref),
        )
        uow.commit()
```

Et il serait enregistre dans le bootstrap :

```python
# src/allocation/service_layer/bootstrap.py

EVENT_HANDLERS: dict[type[events.Event], list] = {
    events.Allocated: [
        handlers.publish_allocated_event,
        handlers.add_allocation_to_read_model,  # <-- mise a jour du read model
    ],
    events.Deallocated: [handlers.reallocate],
    events.OutOfStock: [handlers.send_out_of_stock_notification],
}
```

Le flux complet forme une boucle :

```
   1. Command Allocate arrive
              │
              v
   2. Handler allocate() charge le Product via le repository
              │
              v
   3. Product.allocate() alloue et emet un event Allocated
              │
              v
   4. Message bus collecte l'event Allocated
              │
              v
   5. Handler add_allocation_to_read_model() met a jour allocations_view
              │
              v
   6. GET /allocations/<orderid> lit la table allocations_view
```

### Eventual consistency

Le read model n'est pas mis a jour **dans la meme transaction** que le write
model. Il est mis a jour par un event handler, dans une transaction separee.
Cela signifie qu'il existe un court instant ou le read model n'est pas encore
a jour : c'est l'**eventual consistency**.

!!! warning "Eventual consistency"
    Apres une ecriture, le read model peut avoir un **leger retard** sur le
    write model. Si un utilisateur alloue une commande puis consulte
    immediatement ses allocations, il est possible que le resultat n'apparaisse
    pas encore.

    En pratique, ce delai est de l'ordre de quelques millisecondes dans un
    systeme monolithique. Mais c'est un aspect a garder en tete, surtout
    si vous evoluez vers un systeme distribue (avec Redis ou Kafka entre
    les deux).

L'eventual consistency est le prix a payer pour la separation propre des
responsabilites. Dans la grande majorite des cas, ce compromis est largement
acceptable. Les utilisateurs ne remarquent pas un delai de quelques
millisecondes, et le systeme gagne en clarte, en performance de lecture et
en capacite d'evolution.

---

## Aller plus loin : Deallocated et le read model

Le meme principe s'applique symetriquement aux desallocations. Quand un lot
change de quantite et que des lignes sont desallouees, le domaine emet des
events `Deallocated`. Un handler peut alors nettoyer le read model :

```python
def remove_allocation_from_read_model(
    event: events.Deallocated,
    uow: AbstractUnitOfWork,
) -> None:
    """Supprime une allocation du read model quand une désallocation se produit."""
    with uow:
        uow.session.execute(
            "DELETE FROM allocations_view"
            " WHERE orderid = :orderid AND sku = :sku",
            dict(orderid=event.orderid, sku=event.sku),
        )
        uow.commit()
```

Le read model reste ainsi coherent avec le write model, en reagissant aux
memes events que le reste du systeme. C'est l'un des grands avantages de
l'architecture event-driven : ajouter un nouveau "consommateur" d'events
(ici, la mise a jour du read model) ne modifie en rien les producteurs
d'events (le domaine).

---

## Quand utiliser CQRS

CQRS n'est pas un pattern qu'il faut appliquer partout. C'est un outil
puissant, mais qui ajoute de la complexite : une table supplementaire a
maintenir, des event handlers a ecrire, de l'eventual consistency a gerer.

### CQRS est utile quand :

- **Les patterns de lecture et d'ecriture divergent fortement.** Si le read
  model ressemblerait de toute facon au write model, la separation n'apporte
  pas grand-chose. Mais quand les lectures necessitent des jointures complexes,
  des agregations, ou des projections specifiques, un read model denormalise
  simplifie enormement les choses.

- **Les performances de lecture sont critiques.** Un dashboard qui affiche des
  statistiques en temps reel ne peut pas se permettre de reconstruire des
  aggregats a chaque requete. Un read model pre-calcule resout ce probleme.

- **Le systeme est deja event-driven.** Si vous avez deja un message bus et
  des events, ajouter un handler qui met a jour un read model est trivial.
  L'infrastructure est deja en place.

- **Le ratio lecture/ecriture est fortement desequilibre.** La plupart des
  systemes font beaucoup plus de lectures que d'ecritures. Optimiser le chemin
  de lecture a un impact disproportionne sur les performances globales.

### CQRS est superflu quand :

- **L'application est un simple CRUD.** Si les lectures et ecritures portent
  sur les memes structures, un ORM classique suffit amplement.

- **Le domaine est simple.** Si vous n'avez ni aggregats ni invariants
  complexes, vous n'avez probablement pas besoin de separer les chemins.

- **L'equipe est petite et le systeme jeune.** La complexite ajoutee peut
  ralentir le developpement au debut. Mieux vaut commencer simple et evoluer
  vers CQRS quand le besoin se fait sentir.

!!! tip "Approche progressive"
    On peut adopter CQRS de maniere incrementale. Commencez par utiliser le
    meme ORM pour les lectures et les ecritures, mais dans des modules separes.
    Puis, quand les performances l'exigent, introduisez un read model
    denormalise pour les requetes les plus couteuses. Pas besoin de tout
    separer d'un coup.

---

## Resume

CQRS separe les responsabilites de lecture et d'ecriture en deux chemins
distincts, chacun optimise pour son cas d'usage.

```
   ┌─────────────────────────────────────────────────────────────┐
   │                                                             │
   │                      WRITE PATH                             │
   │                                                             │
   │   Command ──> Message Bus ──> Handler ──> Domain Model      │
   │                                              │              │
   │                                              v              │
   │                                         Repository          │
   │                                              │              │
   │                                              v              │
   │              Event emis ◄──────────── Tables normalisees    │
   │                │                      (write model)         │
   │                v                                            │
   │         Event Handler                                       │
   │                │                                            │
   │                v                                            │
   │   ┌─────────────────────────┐                               │
   │   │  Table denormalisee     │                               │
   │   │  (read model)           │                               │
   │   │  ex: allocations_view   │                               │
   │   └────────────┬────────────┘                               │
   │                │                                            │
   │                v                                            │
   │                                                             │
   │                      READ PATH                              │
   │                                                             │
   │   Query ──> View function ──> SELECT SQL ──> JSON           │
   │                                                             │
   └─────────────────────────────────────────────────────────────┘
```

| Concept | Role | Dans notre code |
|---------|------|-----------------|
| **Command** | Intention d'ecriture | `commands.Allocate` |
| **Write model** | Tables normalisees, protegees par le domaine | `order_lines`, `batches`, `allocations` |
| **Event** | Fait qui s'est produit | `events.Allocated` |
| **Read model** | Table denormalisee, optimisee pour la lecture | `allocations_view` |
| **View** | Fonction de lecture pure, SQL direct | `views.allocations()` |
| **Event handler** | Met a jour le read model en reaction aux events | `add_allocation_to_read_model()` |
| **Eventual consistency** | Le read model peut avoir un leger retard | Delai entre commit write et update read |

!!! tip "A retenir"
    - Le modele de domaine est optimise pour l'ecriture. Ne le forcez pas a servir les lectures.
    - CQRS separe les chemins : commands vers le domaine, queries vers le read model.
    - Le read model est une table denormalisee, mise a jour par des event handlers.
    - Les views sont des fonctions simples : un SELECT SQL, un resultat. Pas de domaine.
    - L'eventual consistency est le prix a payer. Il est presque toujours acceptable.
    - Adoptez CQRS quand les besoins de lecture et d'ecriture divergent. Pas avant.
