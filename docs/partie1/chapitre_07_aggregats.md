# Chapitre 7 -- Aggregates et frontières de coherence

!!! info "Ce que vous allez apprendre"
    - Pourquoi un modele de domaine sans frontieres claires mene a des incoherences
    - Ce qu'est un **Aggregate** et comment il protege les invariants metier
    - Le role de l'**Aggregate Root** comme point d'entree unique
    - Comment choisir les frontieres d'un aggregate
    - Comment le **version_number** et l'**optimistic locking** protegent contre les acces concurrents
    - Comment `Product` devient l'aggregate qui contient les `Batch`

---

## Le probleme : un domaine sans frontieres

Jusqu'ici, notre modele de domaine contient des `OrderLine`, des `Batch` et des regles metier d'allocation. Mais rien n'empeche du code exterieur de manipuler directement un `Batch`, de modifier sa quantite, ou d'allouer une ligne sans passer par une logique centralisee.

Imaginons deux requetes HTTP simultanees qui tentent d'allouer la meme quantite de stock :

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

Sans frontiere de coherence, rien ne garantit que les invariants metier (on ne peut pas allouer plus que le stock disponible) sont respectes. Chaque requete voit un etat valide au moment de la lecture, mais l'etat global devient incoherent apres l'ecriture.

Ce probleme est fondamental : **dans un systeme concurrent, il faut definir explicitement quels objets doivent etre modifies ensemble, dans une meme transaction**.

---

## L'Aggregate : un cluster d'objets coherents

Un **Aggregate** est un regroupement d'objets du domaine qui forment une unite de coherence. Toute modification a l'interieur de l'aggregate doit respecter les invariants metier -- les regles qui doivent **toujours** etre vraies.

Les principes fondamentaux :

- **Un aggregate = une transaction.** On ne modifie qu'un seul aggregate par transaction.
- **Les objets internes sont inaccessibles de l'exterieur.** On ne peut pas aller chercher un `Batch` directement, on passe par l'aggregate.
- **Un seul point d'entree** : l'Aggregate Root.

Dans notre domaine, les invariants sont :

1. On ne peut pas allouer une `OrderLine` a un `Batch` si la quantite disponible est insuffisante.
2. L'allocation doit privilegier les lots en stock, puis ceux avec l'ETA la plus proche.
3. Si la quantite d'un lot change, les allocations en excedent doivent etre desallouees.

Toutes ces regles impliquent de raisonner sur **l'ensemble des `Batch` pour un SKU donne**. C'est donc le `Product` (un SKU et ses lots) qui constitue notre aggregate.

---

## L'Aggregate Root : le point d'entree unique

L'**Aggregate Root** est l'objet par lequel on accede a tout le contenu de l'aggregate. C'est lui qui :

- **Expose les operations metier** (allouer, changer une quantite)
- **Verifie les invariants** avant chaque modification
- **Controle l'acces** aux objets internes (`Batch`, `OrderLine`)

Le code exterieur ne doit jamais manipuler directement un `Batch`. Il demande au `Product` de le faire :

```python
# Correct : passer par l'aggregate root
product.allocate(order_line)

# Incorrect : manipuler un Batch directement
batch = somehow_get_batch("batch-001")
batch.allocate(order_line)  # Aucune garantie de coherence !
```

Cette regle a une consequence directe sur le **Repository** : il manipule des `Product`, pas des `Batch`.

---

## La classe `Product` : notre Aggregate Root

Voici la classe `Product` telle qu'elle apparait dans notre code source
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

Trois attributs meritent une attention particuliere :

| Attribut | Role |
|----------|------|
| `sku` | L'identite de l'aggregate. C'est le SKU du produit. |
| `batches` | Les objets internes a l'aggregate. La liste de tous les lots pour ce SKU. |
| `version_number` | Le compteur de version pour l'optimistic locking (voir plus bas). |

Et une liste `events` qui collecte les domain events emis par les operations metier.

### La methode `allocate()`

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

Observons les responsabilites de cette methode :

1. **Elle trie les lots** (`sorted(self.batches)`) pour appliquer la strategie d'allocation (stock d'abord, puis ETA la plus proche).
2. **Elle trouve le premier lot capable** d'accueillir la ligne (`b.can_allocate(line)`).
3. **Elle gere le cas d'erreur** : si aucun lot ne convient, elle emet un evenement `OutOfStock` au lieu de lever une exception.
4. **Elle incremente le `version_number`** apres chaque allocation reussie.
5. **Elle retourne la reference du lot choisi**, permettant au code appelant de savoir ou l'allocation a ete faite.

Le code appelant (la service layer) n'a aucune connaissance des `Batch` individuels. Il demande simplement au `Product` d'allouer.

### La methode `change_batch_quantity()`

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

Cette methode illustre un scenario plus complexe :

1. Elle retrouve le lot concerne **a l'interieur de l'aggregate** (pas via le repository).
2. Elle modifie la quantite achetee.
3. Si la quantite disponible devient negative, elle **desalloue progressivement** des lignes de commande.
4. Pour chaque ligne desallouee, elle emet un evenement `Deallocated`. Ce sont ces events qui declencheront une reallocation ailleurs dans le systeme (via le message bus, que nous verrons au chapitre 8).

---

## Le Repository manipule des Aggregates

Le repository est l'interface entre le domaine et la persistance. Il doit operer au niveau de l'aggregate, pas au niveau de ses composants internes.

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
- **`get_by_batchref()`** retrouve le `Product` parent a partir d'une reference de lot. Meme ici, c'est l'aggregate entier qui est retourne.
- La methode `seen` (un `set[Product]`) permet de garder une trace de tous les aggregates charges ou ajoutes, ce qui sera utile pour collecter les domain events.

---

## Choisir les frontieres de l'Aggregate

Le choix des frontieres est une decision de conception cruciale. La regle a retenir :

!!! warning "Un aggregate = une transaction = un verrou"
    Chaque aggregate delimite une **transaction**. Pendant qu'une transaction modifie un aggregate, aucune autre transaction ne peut le modifier. C'est ainsi qu'on garantit la coherence.

### Trop gros : le probleme de la contention

Si on faisait un aggregate unique `Entrepot` contenant **tous** les produits et **tous** les lots, chaque allocation verrouillerait l'ensemble du stock. Deux commandes pour des produits differents ne pourraient pas etre traitees en parallele.

### Trop petit : le probleme de l'incoherence

Si chaque `Batch` etait son propre aggregate, on n'aurait aucun moyen de garantir que l'allocation choisit le bon lot. Deux transactions pourraient allouer le meme lot simultanement sans le savoir.

### La bonne granularite : `Product`

Un `Product` regroupe tous les lots pour un SKU donne. C'est le bon compromis :

- **Coherence** : toutes les regles d'allocation pour un SKU sont verifiees dans une seule transaction.
- **Concurrence** : deux allocations pour des SKU differents se font en parallele sans conflit.

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

Meme avec des frontieres d'aggregate bien choisies, il reste un risque : deux transactions concurrentes peuvent tenter de modifier **le meme** `Product` simultanement. Le **version_number** resout ce probleme :

1. Quand on charge un `Product`, on lit son `version_number`.
2. Quand on le sauvegarde, on verifie que le `version_number` en base n'a pas change.
3. Si le numero a change (une autre transaction est passee entre-temps), la sauvegarde echoue.

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

La transaction B echoue car la clause `WHERE version_number=3` ne correspond plus a l'etat en base. C'est le principe de l'**optimistic locking** : on suppose que les conflits sont rares, mais on les detecte au moment du `commit`.

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

Et dans la classe `Product`, chaque appel a `allocate()` incremente le compteur :

```python
batch.allocate(line)
self.version_number += 1
return batch.reference
```

!!! tip "Pourquoi optimiste ?"
    On parle de verrouillage **optimiste** parce qu'on ne pose pas de verrou explicite en base de donnees (pas de `SELECT ... FOR UPDATE`). On laisse les transactions avancer en parallele et on ne detecte le conflit qu'au moment du `commit`. C'est plus performant quand les conflits sont rares.

---

## Les Domain Events emis par l'Aggregate

L'aggregate `Product` ne se contente pas de modifier son etat interne. Il **emet des evenements** qui signalent ce qui s'est passe :

| Evenement | Quand | Declencheur |
|-----------|-------|-------------|
| `OutOfStock` | Aucun lot ne peut accueillir la ligne | `allocate()` |
| `Deallocated` | Une ligne est desallouee suite a un changement de quantite | `change_batch_quantity()` |

Ces evenements sont collectes dans `self.events` et seront publies par l'infrastructure (le Unit of Work et le message bus) apres la transaction. C'est une separation nette entre "ce qui s'est passe" et "ce qu'il faut faire ensuite".

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

## Schema recapitulatif

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

## Resume

Les **Aggregates** sont la reponse du Domain-Driven Design au probleme de la coherence dans un systeme concurrent. En delimitant des frontieres claires, ils permettent de raisonner localement sur les invariants tout en preservant la performance globale du systeme.

| Concept | Ce qu'il faut retenir |
|---------|----------------------|
| **Aggregate** | Un cluster d'objets modifies ensemble dans une seule transaction. |
| **Aggregate Root** | Le point d'entree unique de l'aggregate. Toutes les operations passent par lui. |
| **Invariant** | Une regle metier qui doit toujours etre vraie. L'aggregate la garantit. |
| **Frontiere** | Un aggregate = une transaction = un verrou. Ni trop gros, ni trop petit. |
| **Optimistic Locking** | Le `version_number` detecte les conflits entre transactions concurrentes. |
| **Repository** | Il travaille au niveau de l'aggregate, pas de ses composants internes. |
| **Domain Events** | L'aggregate emet des evenements pour signaler ce qui s'est passe. |

!!! quote "A retenir"
    L'aggregate est la reponse a la question : **"quels objets doivent etre coherents entre eux ?"**. Dans notre domaine, tous les lots d'un meme produit doivent etre coherents, donc `Product` est l'aggregate qui contient les `Batch`. Le `version_number` garantit qu'une seule transaction a la fois peut modifier un `Product` donne.

---

*Chapitre suivant : [Events et le Message Bus](../partie2/chapitre_08_events.md) -- comment les evenements emis par l'aggregate declenchent des actions dans le reste du systeme.*
