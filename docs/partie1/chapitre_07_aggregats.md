# Chapitre 7 -- Agrégats et frontières de cohérence

!!! info "Ce que vous allez apprendre"
    - Pourquoi un modèle de domaine sans frontières claires mène à des incohérences
    - Ce qu'est un **Agrégat** et comment il protège les invariants métier
    - Le rôle de l'**Aggregate Root** comme point d'entrée unique
    - Comment choisir les frontières d'un agrégat
    - Comment le **version_number** et l'**optimistic locking** protègent contre les accès concurrents
    - Comment `Product` devient l'agrégat qui contient les `Batch`

---

## Le problème : un domaine sans frontières

Jusqu'ici, notre modèle de domaine contient des `OrderLine`, des `Batch` et des règles métier d'allocation. Mais rien n'empêche du code extérieur de manipuler directement un `Batch`, de modifier sa quantité, ou d'allouer une ligne sans passer par une logique centralisée.

Imaginons deux requêtes HTTP simultanées qui tentent d'allouer la même quantité de stock :

```
Requete A                          Requete B
    |                                  |
    |  lire batch (dispo = 10)         |
    |                                  |  lire batch (dispo = 10)
    |  allouer 10 unites               |
    |                                  |  allouer 10 unites
    |  sauvegarder (dispo = 0)         |
    |                                  |  sauvegarder (dispo = 0)
    v                                  v

    Resultat : 20 unites allouees pour 10 disponibles !
```

Sans frontière de cohérence, rien ne garantit que les invariants métier (on ne peut pas allouer plus que le stock disponible) sont respectés. Chaque requête voit un état valide au moment de la lecture, mais l'état global devient incohérent après l'écriture.

Ce problème est fondamental : **dans un système concurrent, il faut définir explicitement quels objets doivent être modifiés ensemble, dans une même transaction**.

---

## L'Agrégat : un cluster d'objets cohérents

Un **Agrégat** est un regroupement d'objets du domaine qui forment une unité de cohérence. Toute modification à l'intérieur de l'agrégat doit respecter les invariants métier -- les règles qui doivent **toujours** être vraies.

Les principes fondamentaux :

- **Un agrégat = une transaction.** On ne modifie qu'un seul agrégat par transaction.
- **Les objets internes sont inaccessibles de l'extérieur.** On ne peut pas aller chercher un `Batch` directement, on passe par l'agrégat.
- **Un seul point d'entrée** : l'Aggregate Root.

Dans notre domaine, les invariants sont :

1. On ne peut pas allouer une `OrderLine` à un `Batch` si la quantité disponible est insuffisante.
2. L'allocation doit privilégier les lots en stock, puis ceux avec l'ETA la plus proche.
3. Si la quantité d'un lot change, les allocations en excédent doivent être désallouées.

Toutes ces règles impliquent de raisonner sur **l'ensemble des `Batch` pour un SKU donné**. C'est donc le `Product` (un SKU et ses lots) qui constitue notre agrégat.

---

## L'Aggregate Root : le point d'entrée unique

L'**Aggregate Root** est l'objet par lequel on accède à tout le contenu de l'agrégat. C'est lui qui :

- **Expose les opérations métier** (allouer, changer une quantité)
- **Vérifie les invariants** avant chaque modification
- **Contrôle l'accès** aux objets internes (`Batch`, `OrderLine`)

Le code extérieur ne doit jamais manipuler directement un `Batch`. Il demande au `Product` de le faire :

```python
# Correct : passer par l'aggregate root
product.allocate(order_line)

# Incorrect : manipuler un Batch directement
batch = somehow_get_batch("batch-001")
batch.allocate(order_line)  # Aucune garantie de coherence !
```

Cette règle a une conséquence directe sur le **Repository** : il manipule des `Product`, pas des `Batch`.

---

## La classe `Product` : notre Aggregate Root

Voici la classe `Product` telle qu'elle apparaît dans notre code source
(`src/allocation/domain/model.py`) :

```python
class Product:
    """
    Agregat racine pour la gestion des produits.

    Un Product regroupe tous les Batch pour un SKU donne.
    C'est la frontiere de coherence : toutes les operations
    d'allocation passent par cet agregat.
    """

    def __init__(
        self,
        sku: str,
        batches: Optional[list[Batch]] = None,
        version_number: int = 0,
    ):
        self.sku = sku
        self.batches = batches or []
        self.version_number = version_number
        self.events: list[events.Event] = []
```

Trois attributs méritent une attention particulière :

| Attribut | Rôle |
|----------|------|
| `sku` | L'identité de l'agrégat. C'est le SKU du produit. |
| `batches` | Les objets internes à l'agrégat. La liste de tous les lots pour ce SKU. |
| `version_number` | Le compteur de version pour l'optimistic locking (voir plus bas). |

Et une liste `events` qui collecte les domain events émis par les opérations métier.

### La méthode `allocate()`

```python
def allocate(self, line: OrderLine) -> str:
    """
    Alloue une ligne de commande au lot le plus approprie.

    La strategie d'allocation privilegie les lots en stock
    (sans ETA) puis les lots avec l'ETA la plus proche.

    Retourne la reference du lot choisi.
    Emet un evenement OutOfStock s'il n'y a plus de stock.
    """
    try:
        batch = next(
            b for b in sorted(self.batches)
            if b.can_allocate(line)
        )
    except StopIteration:
        self.events.append(events.OutOfStock(sku=line.sku))
        return ""

    batch.allocate(line)
    self.version_number += 1
    return batch.reference
```

Observons les responsabilités de cette méthode :

1. **Elle trie les lots** (`sorted(self.batches)`) pour appliquer la stratégie d'allocation (stock d'abord, puis ETA la plus proche).
2. **Elle trouve le premier lot capable** d'accueillir la ligne (`b.can_allocate(line)`).
3. **Elle gère le cas d'erreur** : si aucun lot ne convient, elle émet un événement `OutOfStock` au lieu de lever une exception.
4. **Elle incrémente le `version_number`** après chaque allocation réussie.
5. **Elle retourne la référence du lot choisi**, permettant au code appelant de savoir où l'allocation a été faite.

Le code appelant (la service layer) n'a aucune connaissance des `Batch` individuels. Il demande simplement au `Product` d'allouer.

### La méthode `change_batch_quantity()`

```python
def change_batch_quantity(self, ref: str, qty: int) -> None:
    """
    Modifie la quantite d'un lot et realloue si necessaire.

    Si la nouvelle quantite est inferieure aux allocations existantes,
    les lignes en excedent sont desallouees et des evenements
    Deallocated sont emis pour chacune.
    """
    batch = next(b for b in self.batches if b.reference == ref)
    batch._purchased_quantity = qty
    while batch.available_quantity < 0:
        line = batch.deallocate_one()
        self.events.append(
            events.Deallocated(
                orderid=line.orderid,
                sku=line.sku,
                qty=line.qty,
            )
        )
```

Cette méthode illustre un scénario plus complexe :

1. Elle retrouve le lot concerné **à l'intérieur de l'agrégat** (pas via le repository).
2. Elle modifie la quantité achetée.
3. Si la quantité disponible devient négative, elle **désalloue progressivement** des lignes de commande.
4. Pour chaque ligne désallouée, elle émet un événement `Deallocated`. Ce sont ces events qui déclencheront une réallocation ailleurs dans le système (via le message bus, que nous verrons au chapitre 8).

---

## Le Repository manipule des Agrégats

Le repository est l'interface entre le domaine et la persistance. Il doit opérer au niveau de l'agrégat, pas au niveau de ses composants internes.

```python
class AbstractRepository(abc.ABC):

    def add(self, product: model.Product) -> None:
        """Ajoute un produit au repository."""
        ...

    def get(self, sku: str) -> model.Product | None:
        """Recupere un produit par son SKU."""
        ...

    def get_by_batchref(self, batchref: str) -> model.Product | None:
        """Recupere un produit contenant le batch de reference donnee."""
        ...
```

On remarque :

- **`add()` et `get()` travaillent avec des `Product`**, jamais des `Batch`.
- **`get_by_batchref()`** retrouve le `Product` parent à partir d'une référence de lot. Même ici, c'est l'agrégat entier qui est retourné.
- La méthode `seen` (un `set[Product]`) permet de garder une trace de tous les agrégats chargés ou ajoutés, ce qui sera utile pour collecter les domain events.

---

## Choisir les frontières de l'Agrégat

Le choix des frontières est une décision de conception cruciale. La règle à retenir :

!!! warning "Un agrégat = une transaction = un verrou"
    Chaque agrégat délimite une **transaction**. Pendant qu'une transaction modifie un agrégat, aucune autre transaction ne peut le modifier. C'est ainsi qu'on garantit la cohérence.

### Trop gros : le problème de la contention

Si on faisait un agrégat unique `Entrepôt` contenant **tous** les produits et **tous** les lots, chaque allocation verrouillerait l'ensemble du stock. Deux commandes pour des produits différents ne pourraient pas être traitées en parallèle.

### Trop petit : le problème de l'incohérence

Si chaque `Batch` était son propre agrégat, on n'aurait aucun moyen de garantir que l'allocation choisit le bon lot. Deux transactions pourraient allouer le même lot simultanément sans le savoir.

### La bonne granularité : `Product`

Un `Product` regroupe tous les lots pour un SKU donné. C'est le bon compromis :

- **Cohérence** : toutes les règles d'allocation pour un SKU sont vérifiées dans une seule transaction.
- **Concurrence** : deux allocations pour des SKU différents se font en parallèle sans conflit.

```
+------------------+   +------------------+
|  Product (SKU-A) |   |  Product (SKU-B) |
|  +-------+       |   |  +-------+       |
|  |Batch 1|       |   |  |Batch 3|       |
|  +-------+       |   |  +-------+       |
|  +-------+       |   |  +-------+       |
|  |Batch 2|       |   |  |Batch 4|       |
|  +-------+       |   |  +-------+       |
+------------------+   +------------------+
  ^ transaction A        ^ transaction B
  (independantes)
```

---

## Le versioning optimiste (Optimistic Locking)

Même avec des frontières d'agrégat bien choisies, il reste un risque : deux transactions concurrentes peuvent tenter de modifier **le même** `Product` simultanément. Le **version_number** résout ce problème :

1. Quand on charge un `Product`, on lit son `version_number`.
2. Quand on le sauvegarde, on vérifie que le `version_number` en base n'a pas changé.
3. Si le numéro a changé (une autre transaction est passée entre-temps), la sauvegarde échoue.

```
Transaction A                       Transaction B
     |                                   |
     | SELECT ... => version = 3         |
     |                                   | SELECT ... => version = 3
     | allocate(...) => version = 4      |
     |                                   | allocate(...) => version = 4
     | UPDATE ... WHERE version=3        |
     | => OK (1 row)                     |
     |                                   | UPDATE ... WHERE version=3
     |                                   | => ECHEC (0 rows)
     v                                   v
```

La transaction B échoue car la clause `WHERE version_number=3` ne correspond plus à l'état en base. C'est le principe de l'**optimistic locking** : on suppose que les conflits sont rares, mais on les détecte au moment du `commit`.

Dans le mapping ORM (`src/allocation/adapters/orm.py`), la table `products` porte ce champ :

```python
products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sku", String(255)),
    Column("version_number", Integer, nullable=False, server_default="0"),
)
```

Et dans la classe `Product`, chaque appel à `allocate()` incrémente le compteur :

```python
batch.allocate(line)
self.version_number += 1
return batch.reference
```

!!! tip "Pourquoi optimiste ?"
    On parle de verrouillage **optimiste** parce qu'on ne pose pas de verrou explicite en base de données (pas de `SELECT ... FOR UPDATE`). On laisse les transactions avancer en parallèle et on ne détecte le conflit qu'au moment du `commit`. C'est plus performant quand les conflits sont rares.

---

## Les Domain Events émis par l'Agrégat

L'agrégat `Product` ne se contente pas de modifier son état interne. Il **émet des événements** qui signalent ce qui s'est passé :

| Événement | Quand | Déclencheur |
|-----------|-------|-------------|
| `OutOfStock` | Aucun lot ne peut accueillir la ligne | `allocate()` |
| `Deallocated` | Une ligne est désallouée suite à un changement de quantité | `change_batch_quantity()` |

Ces événements sont collectés dans `self.events` et seront publiés par l'infrastructure (le Unit of Work et le message bus) après la transaction. C'est une séparation nette entre "ce qui s'est passé" et "ce qu'il faut faire ensuite".

```python
# Dans Product.__init__
self.events: list[events.Event] = []

# Dans allocate(), si rupture de stock :
self.events.append(events.OutOfStock(sku=line.sku))

# Dans change_batch_quantity(), pour chaque ligne desallouee :
self.events.append(
    events.Deallocated(orderid=line.orderid, sku=line.sku, qty=line.qty)
)
```

---

## Schéma récapitulatif

```
+------------------------------------------------------------+
|                                                            |
|   Aggregate : Product                                      |
|                                                            |
|   Identite :  sku = "BLUE-VASE"                            |
|   Version :   version_number = 3                           |
|   Events :    [OutOfStock(...), Deallocated(...)]          |
|                                                            |
|   +------------------------+  +------------------------+   |
|   |  Batch                 |  |  Batch                 |   |
|   |  reference: "batch-01" |  |  reference: "batch-02" |   |
|   |  sku: "BLUE-VASE"     |  |  sku: "BLUE-VASE"      |   |
|   |  qty: 100             |  |  qty: 50               |   |
|   |  eta: None (en stock) |  |  eta: 2025-03-15       |   |
|   |                        |  |                        |   |
|   |  _allocations:         |  |  _allocations:         |   |
|   |    {OrderLine(...),    |  |    {OrderLine(...)}    |   |
|   |     OrderLine(...)}    |  |                        |   |
|   +------------------------+  +------------------------+   |
|                                                            |
+------------------------------------------------------------+
         ^
         |
    Aggregate Root : le seul point d'acces
    Repository.get("BLUE-VASE") -> Product
```

---

## Résumé

Les **Agrégats** sont la réponse du Domain-Driven Design au problème de la cohérence dans un système concurrent. En délimitant des frontières claires, ils permettent de raisonner localement sur les invariants tout en préservant la performance globale du système.

| Concept | Ce qu'il faut retenir |
|---------|----------------------|
| **Agrégat** | Un cluster d'objets modifiés ensemble dans une seule transaction. |
| **Aggregate Root** | Le point d'entrée unique de l'agrégat. Toutes les opérations passent par lui. |
| **Invariant** | Une règle métier qui doit toujours être vraie. L'agrégat la garantit. |
| **Frontière** | Un agrégat = une transaction = un verrou. Ni trop gros, ni trop petit. |
| **Optimistic Locking** | Le `version_number` détecte les conflits entre transactions concurrentes. |
| **Repository** | Il travaille au niveau de l'agrégat, pas de ses composants internes. |
| **Domain Events** | L'agrégat émet des événements pour signaler ce qui s'est passé. |

!!! quote "À retenir"
    L'agrégat est la réponse à la question : **"quels objets doivent être cohérents entre eux ?"**. Dans notre domaine, tous les lots d'un même produit doivent être cohérents, donc `Product` est l'agrégat qui contient les `Batch`. Le `version_number` garantit qu'une seule transaction à la fois peut modifier un `Product` donné.

---

*Chapitre suivant : [Events et le Message Bus](../partie2/chapitre_08_events.md) -- comment les événements émis par l'agrégat déclenchent des actions dans le reste du système.*
