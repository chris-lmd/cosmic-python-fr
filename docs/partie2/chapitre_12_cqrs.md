# Chapitre 12 -- CQRS (Command Query Responsibility Segregation)

!!! info "Avant / Après"

    | | |
    |---|---|
    | **Avant** | Même modèle pour lecture et écriture |
    | **Après** | Write model normalisé + Read model dénormalisé |

## Le problème de la lecture

Dans les chapitres précédents, nous avons construit un modèle de domaine riche :
des agrégats (`Produit`), des entités (`Lot`), des value objects (`LigneDeCommande`),
des invariants métier, un repository pour la persistance et un message bus pour
l'orchestration. Tout cela forme un chemin d'écriture solide et bien protégé.

Mais posons-nous une question simple : que se passe-t-il quand un utilisateur
veut simplement **afficher** les allocations d'une commande ?

Avec notre architecture actuelle, le chemin ressemblerait à ceci :

```
   Requête GET /allocations/order-123
        │
        v
   Repository.get(sku=...)          # Charge un Produit entier
        │
        v
   Produit                           # Avec tous ses Lot
     ├── Lot("batch-001")            # Chaque Lot avec ses allocations
     │     └── {LigneDeCommande, LigneDeCommande, ...}
     ├── Lot("batch-002")
     │     └── {LigneDeCommande, LigneDeCommande, ...}
     └── Lot("batch-003")
           └── {LigneDeCommande, ...}
        │
        v
   Parcours de toutes les allocations pour trouver celles de "order-123"
        │
        v
   Sérialisation en JSON
```

Pour répondre à une question triviale -- "quels SKUs sont alloués à cette
commande ?" -- on charge un agrégat complet avec tous ses lots, toutes ses
allocations, on reconstruit le graphe d'objets, on traverse les relations...
C'est comme sortir toute la bibliothèque pour trouver un seul livre.

Le modèle de domaine est optimisé pour **protéger les invariants en écriture** :

- L'agrégat `Produit` garantit qu'on n'alloue pas plus que le stock disponible.
- Le numéro de version protège contre les accès concurrents.
- Les événements tracent chaque changement d'état.

Mais pour la **lecture**, on n'a besoin d'aucune de ces garanties. Pas
d'invariants à vérifier, pas de concurrence à gérer, pas d'events à émettre.
On veut juste des données, le plus vite possible.

!!! note "Le constat fondamental"
    Les besoins de lecture et d'écriture sont **fondamentalement différents**.
    Utiliser le même modèle pour les deux, c'est faire un compromis qui pénalise
    les deux côtés.

---

## Le principe CQRS

CQRS -- Command Query Responsibility Segregation -- propose une solution
radicale : **séparer complètement les chemins d'écriture et de lecture**.

L'idée vient du principe CQS (Command Query Separation) de Bertrand Meyer,
appliqué à l'échelle de l'architecture :

- Les **commands** (écriture) passent par le domaine, le message bus, le
  repository. Elles modifient l'état du système.
- Les **queries** (lecture) interrogent directement la base de données.
  Elles ne modifient rien.

```
   CQRS : deux chemins distincts pour deux besoins distincts.

   ┌──────────────────────────────────────────────────────────────────┐
   │                          API Flask                               │
   │                                                                  │
   │   POST /allocate              GET /allocations/<id_commande>     │
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

À gauche, le chemin d'écriture traverse toute la pile : validation, logique
métier, persistance via le repository, émission d'events. À droite, le chemin
de lecture va droit au but : une fonction, une requête SQL, un résultat.

La clé de CQRS, c'est qu'on utilise **deux modèles différents** pour deux
besoins différents :

| Aspect              | Write model                         | Read model                       |
|---------------------|-------------------------------------|----------------------------------|
| **Objectif**        | Protéger les invariants métier      | Servir des données rapidement    |
| **Structure**       | Agrégats, entités, value objects   | Tables dénormalisées, vues       |
| **Accès**           | Via repository + domain model       | Via SQL direct                   |
| **Complexité**      | Riche (logique métier)              | Simple (projection de données)   |
| **Optimisé pour**   | Cohérence et règles métier          | Performance de lecture            |

---

## Le read model : `allocations_view`

Le read model est une table **dénormalisée** conçue spécifiquement pour
répondre à une question de lecture. Contrairement aux tables du write model
(qui sont normalisées avec des clés étrangères et des jointures), le read
model contient exactement les colonnes dont la vue a besoin, dans un format
directement exploitable.

Dans notre ORM, la table `allocations_view` est définie ainsi :

```python
# src/allocation/adapters/orm.py

allocations_view = Table(
    "allocations_view",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("id_commande", String(255)),
    Column("sku", String(255)),
    Column("réf_lot", String(255)),
)
```

Comparons avec les tables du write model, qui nécessiteraient des jointures
pour obtenir la même information :

```
   WRITE MODEL (normalisé)               READ MODEL (dénormalisé)
   ─────────────────────────             ─────────────────────────

   order_lines                            allocations_view
   ┌────────────────┬──────┬──────────┐   ┌──────────────┬──────┬──────────┐
   │id_commande     │ sku  │ quantité │   │ id_commande  │ sku  │ réf_lot  │
   ├────────────────┼──────┼──────────┤   ├──────────────┼──────┼──────────┤
   │order-1         │LAMP  │  10      │   │ order-1      │LAMP  │ batch-01 │
   └────┬───────────┴──────┴──────────┘   │ order-1      │TABLE │ batch-03 │
        │                                 │ order-2      │LAMP  │ batch-02 │
        │  allocations                    └──────────────┴──────┴──────────┘
        │  ┌────────────┬──────────┐
        └──│orderline_id│ batch_id │      Pas de jointure. Pas de
           ├────────────┼──────────┤      reconstruction d'objet.
           │     1      │    3     │      Tout est déjà prêt à lire.
           └────────────┴──────────┘
                             │
        lots                 │
        ┌────┬──────────┬────┘
        │ id │référence │ ...
        ├────┼──────────┤
        │  3 │batch-01  │
        └────┴──────────┘

   Pour obtenir "quels SKUs sont alloués à order-1", le write model
   exige 3 tables et 2 jointures. Le read model : 1 table, 0 jointure.
```

Le read model duplique de l'information -- oui, c'est volontaire. En
dénormalisant, on échange de l'espace disque (bon marché) contre de la vitesse
de lecture (précieuse). C'est un compromis classique et parfaitement justifié
pour les chemins de lecture.

---

## Les views : des fonctions de lecture pure

Le côté query de CQRS est implémenté par des **views** : des fonctions simples
qui exécutent des requêtes SQL directes sur le read model. Pas de domaine, pas
de repository, pas d'agrégat. Juste une requête et un résultat.

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


def allocations(id_commande: str, uow: unit_of_work.AbstractUnitOfWork) -> list[dict]:
    """
    Retourne les allocations pour un id_commande donné.

    Requête SQL directe sur la table de lecture (read model).
    """
    with uow:
        results = uow.session.execute(
            text("SELECT sku, réf_lot FROM allocations_view WHERE id_commande = :id_commande"),
            dict(id_commande=id_commande),
        )
        return [dict(r._mapping) for r in results]
```

Remarquez à quel point c'est simple. La fonction `allocations` :

1. Ouvre une session via le unit of work.
2. Exécute une requête SQL brute sur `allocations_view`.
3. Retourne une liste de dictionnaires.

Pas de `Produit`, pas de `Lot`, pas de `LigneDeCommande`. Pas de reconstruction
d'agrégat, pas de traversée de relations. La requête va directement chercher
les données là où elles sont, dans le format exact dont l'API a besoin.

Le endpoint Flask qui utilise cette view est tout aussi direct :

```python
# src/allocation/entrypoints/flask_app.py

@app.route("/allocations/<id_commande>", methods=["GET"])
def allocations_view_endpoint(id_commande: str):
    """
    GET /allocations/<id_commande>

    Retourne les allocations pour une commande donnée (lecture CQRS).
    """
    from allocation.views import views

    result = views.allocations(id_commande, bus.uow)
    if not result:
        return "not found", 404
    return jsonify(result), 200
```

Le contraste avec les endpoints d'écriture est frappant :

| Endpoint d'écriture (`POST /allocate`)          | Endpoint de lecture (`GET /allocations`) |
|--------------------------------------------------|------------------------------------------|
| Construit une `Command`                          | Appelle une view directement             |
| Envoie au message bus                            | Pas de message bus                       |
| Le handler charge un agrégat via le repository  | La view fait un `SELECT` SQL             |
| Le domaine vérifie les invariants                | Aucune vérification métier               |
| Des events sont émis                             | Aucun event                              |
| Le résultat est un effet de bord (allocation)    | Le résultat est une projection de données |

---

## Mise à jour du read model par les event handlers

Si le read model est une table séparée, comment reste-t-il synchronisé avec le
write model ? Par les **event handlers**. Quand une allocation est effectuée, le
domaine émet un event `Alloué`. Un handler écoute cet event et met à jour la
table `allocations_view`.

Voici comment l'event `Alloué` est défini :

```python
# src/allocation/domain/events.py

@dataclass(frozen=True)
class Alloué(Event):
    """Une LigneDeCommande a été allouée à un Lot."""

    id_commande: str
    sku: str
    quantité: int
    réf_lot: str
```

L'event contient toutes les informations nécessaires pour mettre à jour le read
model : le `id_commande`, le `sku` et le `réf_lot`. C'est exactement ce que la
table `allocations_view` attend.

Le handler de mise à jour du read model ressemblerait à ceci :

```python
# src/allocation/service_layer/handlers.py

from sqlalchemy import text

def ajouter_allocation_vue(
    event: events.Alloué,
    uow: AbstractUnitOfWork,
) -> None:
    """Met à jour le read model quand une allocation est effectuée."""
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

Et il serait enregistré dans le bootstrap :

```python
# src/allocation/service_layer/bootstrap.py

EVENT_HANDLERS: dict[type[events.Event], list] = {
    events.Alloué: [
        handlers.publier_événement_allocation,
        handlers.ajouter_allocation_vue,  # <-- mise à jour du read model
    ],
    events.Désalloué: [handlers.réallouer],
    events.RuptureDeStock: [handlers.envoyer_notification_rupture_stock],
}
```

Le flux complet forme une boucle :

```
   1. Command Allouer arrive
              │
              v
   2. Handler allouer() charge le Produit via le repository
              │
              v
   3. Produit.allouer() alloue et émet un event Alloué
              │
              v
   4. Message bus collecte l'event Alloué
              │
              v
   5. Handler ajouter_allocation_vue() met à jour allocations_view
              │
              v
   6. GET /allocations/<id_commande> lit la table allocations_view
```

### Eventual consistency

Le read model n'est pas mis à jour **dans la même transaction** que le write
model. Il est mis à jour par un event handler, dans une transaction séparée.
Cela signifie qu'il existe un court instant où le read model n'est pas encore
à jour : c'est l'**eventual consistency**.

!!! warning "Eventual consistency"
    Après une écriture, le read model peut avoir un **léger retard** sur le
    write model. Si un utilisateur alloue une commande puis consulte
    immédiatement ses allocations, il est possible que le résultat n'apparaisse
    pas encore.

    En pratique, ce délai est de l'ordre de quelques millisecondes dans un
    système monolithique. Mais c'est un aspect à garder en tête, surtout
    si vous évoluez vers un système distribué (avec Redis ou Kafka entre
    les deux).

L'eventual consistency est le prix à payer pour la séparation propre des
responsabilités. Dans la grande majorité des cas, ce compromis est largement
acceptable. Les utilisateurs ne remarquent pas un délai de quelques
millisecondes, et le système gagne en clarté, en performance de lecture et
en capacité d'évolution.

---

## Aller plus loin : Désalloué et le read model

Le même principe s'applique symétriquement aux désallocations. Quand un lot
change de quantité et que des lignes sont désallouées, le domaine émet des
events `Désalloué`. Un handler peut alors nettoyer le read model :

```python
def supprimer_allocation_vue(
    event: events.Désalloué,
    uow: AbstractUnitOfWork,
) -> None:
    """Supprime une allocation du read model quand une désallocation se produit."""
    with uow:
        uow.session.execute(
            text(
                "DELETE FROM allocations_view"
                " WHERE id_commande = :id_commande AND sku = :sku"
            ),
            dict(id_commande=event.id_commande, sku=event.sku),
        )
        uow.commit()
```

Le read model reste ainsi cohérent avec le write model, en réagissant aux
mêmes events que le reste du système. C'est l'un des grands avantages de
l'architecture event-driven : ajouter un nouveau "consommateur" d'events
(ici, la mise à jour du read model) ne modifie en rien les producteurs
d'events (le domaine).

---

## Quand utiliser CQRS

CQRS n'est pas un pattern qu'il faut appliquer partout. C'est un outil
puissant, mais qui ajoute de la complexité : une table supplémentaire à
maintenir, des event handlers à écrire, de l'eventual consistency à gérer.

### CQRS est utile quand :

- **Les patterns de lecture et d'écriture divergent fortement.** Si le read
  model ressemblerait de toute façon au write model, la séparation n'apporte
  pas grand-chose. Mais quand les lectures nécessitent des jointures complexes,
  des agrégations, ou des projections spécifiques, un read model dénormalisé
  simplifie énormément les choses.

- **Les performances de lecture sont critiques.** Un dashboard qui affiche des
  statistiques en temps réel ne peut pas se permettre de reconstruire des
  agrégats à chaque requête. Un read model pré-calculé résout ce problème.

- **Le système est déjà event-driven.** Si vous avez déjà un message bus et
  des events, ajouter un handler qui met à jour un read model est trivial.
  L'infrastructure est déjà en place.

- **Le ratio lecture/écriture est fortement déséquilibré.** La plupart des
  systèmes font beaucoup plus de lectures que d'écritures. Optimiser le chemin
  de lecture a un impact disproportionné sur les performances globales.

### CQRS est superflu quand :

- **L'application est un simple CRUD.** Si les lectures et écritures portent
  sur les mêmes structures, un ORM classique suffit amplement.

- **Le domaine est simple.** Si vous n'avez ni agrégats ni invariants
  complexes, vous n'avez probablement pas besoin de séparer les chemins.

- **L'équipe est petite et le système jeune.** La complexité ajoutée peut
  ralentir le développement au début. Mieux vaut commencer simple et évoluer
  vers CQRS quand le besoin se fait sentir.

!!! tip "Approche progressive"
    On peut adopter CQRS de manière incrémentale. Commencez par utiliser le
    même ORM pour les lectures et les écritures, mais dans des modules séparés.
    Puis, quand les performances l'exigent, introduisez un read model
    dénormalisé pour les requêtes les plus coûteuses. Pas besoin de tout
    séparer d'un coup.

---

## Résumé

CQRS sépare les responsabilités de lecture et d'écriture en deux chemins
distincts, chacun optimisé pour son cas d'usage.

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
   │              Event émis ◄──────────── Tables normalisées    │
   │                │                      (write model)         │
   │                v                                            │
   │         Event Handler                                       │
   │                │                                            │
   │                v                                            │
   │   ┌─────────────────────────┐                               │
   │   │  Table dénormalisée     │                               │
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

| Concept | Rôle | Dans notre code |
|---------|------|-----------------|
| **Command** | Intention d'écriture | `commands.Allouer` |
| **Write model** | Tables normalisées, protégées par le domaine | `order_lines`, `lots`, `allocations` |
| **Event** | Fait qui s'est produit | `events.Alloué` |
| **Read model** | Table dénormalisée, optimisée pour la lecture | `allocations_view` |
| **View** | Fonction de lecture pure, SQL direct | `views.allocations()` |
| **Event handler** | Met à jour le read model en réaction aux events | `ajouter_allocation_vue()` |
| **Eventual consistency** | Le read model peut avoir un léger retard | Délai entre commit write et update read |

## Exercices

!!! example "Exercice 1 -- Nouvelle vue"
    Créez un read model `stock_view` qui montre la quantité disponible par SKU. Définissez la table, l'event handler qui la met à jour (sur `Alloué` et `Désalloué`), et la fonction de lecture.

!!! example "Exercice 2 -- Rebuild du read model"
    Si la table `allocations_view` est corrompue, comment la reconstruire à partir des tables du write model ? Écrivez un script SQL qui le fait.

!!! example "Exercice 3 -- Tester l'eventual consistency"
    Écrivez un test qui vérifie que après un `bus.handle(commands.Allouer(...))`, la vue `allocations` retourne bien l'allocation. Ce test prouve-t-il la consistency ou l'eventual consistency ?

---

!!! tip "À retenir"
    - Le modèle de domaine est optimisé pour l'écriture. Ne le forcez pas à servir les lectures.
    - CQRS sépare les chemins : commands vers le domaine, queries vers le read model.
    - Le read model est une table dénormalisée, mise à jour par des event handlers.
    - Les views sont des fonctions simples : un SELECT SQL, un résultat. Pas de domaine.
    - L'eventual consistency est le prix à payer. Il est presque toujours acceptable.
    - Adoptez CQRS quand les besoins de lecture et d'écriture divergent. Pas avant.
