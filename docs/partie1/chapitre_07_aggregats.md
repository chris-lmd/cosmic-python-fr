# Chapitre 7 -- Agrégats et frontières de cohérence

!!! info "Ce que vous allez apprendre"
    - Pourquoi un modèle de domaine sans frontières claires mène à des incohérences
    - Ce qu'est un **Agrégat** et comment il protège les invariants métier
    - Le rôle de l'**Aggregate Root** comme point d'entrée unique
    - Comment choisir les frontières d'un agrégat
    - Comment le **numéro_version** et l'**optimistic locking** protègent contre les accès concurrents
    - Comment `Produit` devient l'agrégat qui contient les `Lot`

---

## Le problème : un domaine sans frontières

Au chapitre 1, nous avons défini une fonction libre `allouer(ligne, lots)` qui prend une liste de lots et choisit le meilleur. C'est simple et lisible, mais cela pose un problème fondamental : **qui est responsable de fournir la bonne liste de lots ?** Rien n'empêche du code extérieur de manipuler directement un `Lot`, de modifier sa quantité, ou d'allouer une ligne sans passer par cette stratégie.

Imaginons deux requêtes HTTP simultanées qui tentent d'allouer la même quantité de stock :

```
Requete A                          Requete B
    |                                  |
    |  lire lot (dispo = 10)           |
    |                                  |  lire lot (dispo = 10)
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
- **Les objets internes sont inaccessibles de l'extérieur.** On ne peut pas aller chercher un `Lot` directement, on passe par l'agrégat.
- **Un seul point d'entrée** : l'Aggregate Root.

Dans notre domaine, les invariants sont :

1. On ne peut pas allouer une `LigneDeCommande` à un `Lot` si la quantité disponible est insuffisante.
2. L'allocation doit privilégier les lots en stock, puis ceux avec l'ETA la plus proche.
3. Si la quantité d'un lot change, les allocations en excédent doivent être désallouées.

Toutes ces règles impliquent de raisonner sur **l'ensemble des `Lot` pour un SKU donné**. C'est donc le `Produit` (un SKU et ses lots) qui constitue notre agrégat.

---

## L'Aggregate Root : le point d'entrée unique

L'**Aggregate Root** est l'objet par lequel on accède à tout le contenu de l'agrégat. C'est lui qui :

- **Expose les opérations métier** (allouer, modifier une quantité)
- **Vérifie les invariants** avant chaque modification
- **Contrôle l'accès** aux objets internes (`Lot`, `LigneDeCommande`)

Le code extérieur ne doit jamais manipuler directement un `Lot`. Il demande au `Produit` de le faire :

```python
# Correct : passer par l'aggregate root
produit.allouer(ligne_de_commande)

# Incorrect : manipuler un Lot directement
lot = somehow_get_lot("lot-001")
lot.allouer(ligne_de_commande)  # Aucune garantie de coherence !
```

Cette règle a une conséquence directe sur le **Repository** : il manipule des `Produit`, pas des `Lot`.

---

## La classe `Produit` : notre Aggregate Root

La fonction libre `allouer(ligne, lots)` du chapitre 1 va maintenant **devenir une méthode** de `Produit`. L'agrégat possède les lots et prend la responsabilité de la stratégie d'allocation. La différence clé : au lieu de lever une exception `RuptureDeStock`, l'agrégat **émet un événement** (nous verrons pourquoi au chapitre 8).

Voici la classe `Produit` telle qu'elle apparaît dans notre code source
(`src/allocation/domain/model.py`) :

```python
class Produit:
    """
    Agregat racine pour la gestion des produits.

    Un Produit regroupe tous les Lot pour un SKU donne.
    C'est la frontiere de coherence : toutes les operations
    d'allocation passent par cet agregat.
    """

    def __init__(
        self,
        sku: str,
        lots: Optional[list[Lot]] = None,
        numéro_version: int = 0,
    ):
        self.sku = sku
        self.lots = lots or []
        self.numéro_version = numéro_version
        self.événements: list[events.Event] = []
```

Trois attributs méritent une attention particulière :

| Attribut | Rôle |
|----------|------|
| `sku` | L'identité de l'agrégat. C'est le SKU du produit. |
| `lots` | Les objets internes à l'agrégat. La liste de tous les lots pour ce SKU. |
| `numéro_version` | Le compteur de version pour l'optimistic locking (voir plus bas). |

Et une liste `événements` qui collecte les domain events émis par les opérations métier.

### La méthode `allouer()`

```python
def allouer(self, ligne: LigneDeCommande) -> str:
    """
    Alloue une ligne de commande au lot le plus approprie.

    La strategie d'allocation privilegie les lots en stock
    (sans ETA) puis les lots avec l'ETA la plus proche.

    Retourne la reference du lot choisi.
    Emet un evenement RuptureDeStock s'il n'y a plus de stock.
    """
    try:
        lot = next(
            l for l in sorted(self.lots)
            if l.peut_allouer(ligne)
        )
    except StopIteration:
        self.événements.append(events.RuptureDeStock(sku=ligne.sku))
        return ""

    lot.allouer(ligne)
    self.numéro_version += 1
    return lot.référence
```

Observons les responsabilités de cette méthode :

1. **Elle trie les lots** (`sorted(self.lots)`) pour appliquer la stratégie d'allocation (stock d'abord, puis ETA la plus proche).
2. **Elle trouve le premier lot capable** d'accueillir la ligne (`l.peut_allouer(ligne)`).
3. **Elle gère le cas d'erreur** : si aucun lot ne convient, elle émet un événement `RuptureDeStock` au lieu de lever une exception.
4. **Elle incrémente le `numéro_version`** après chaque allocation réussie.
5. **Elle retourne la référence du lot choisi**, permettant au code appelant de savoir où l'allocation a été faite.

Le code appelant (la service layer) n'a aucune connaissance des `Lot` individuels. Il demande simplement au `Produit` d'allouer.

### La méthode `modifier_quantité_lot()`

```python
def modifier_quantité_lot(self, réf: str, quantité: int) -> None:
    """
    Modifie la quantite d'un lot et realloue si necessaire.

    Si la nouvelle quantite est inferieure aux allocations existantes,
    les lignes en excedent sont desallouees et des evenements
    Désalloué sont emis pour chacune.
    """
    lot = next(l for l in self.lots if l.référence == réf)
    lot._quantité_achetée = quantité
    while lot.quantité_disponible < 0:
        ligne = lot.désallouer_une()
        self.événements.append(
            events.Désalloué(
                id_commande=ligne.id_commande,
                sku=ligne.sku,
                quantité=ligne.quantité,
            )
        )
```

Cette méthode illustre un scénario plus complexe :

1. Elle retrouve le lot concerné **à l'intérieur de l'agrégat** (pas via le repository).
2. Elle modifie la quantité achetée.
3. Si la quantité disponible devient négative, elle **désalloue progressivement** des lignes de commande.
4. Pour chaque ligne désallouée, elle émet un événement `Désalloué`. Ce sont ces events qui déclencheront une réallocation ailleurs dans le système (via le message bus, que nous verrons au chapitre 8).

---

## Le Repository manipule des Agrégats

Le repository est l'interface entre le domaine et la persistance. Il doit opérer au niveau de l'agrégat, pas au niveau de ses composants internes.

```python
class AbstractRepository(abc.ABC):

    def add(self, produit: model.Produit) -> None:
        """Ajoute un produit au repository."""
        ...

    def get(self, sku: str) -> model.Produit | None:
        """Recupere un produit par son SKU."""
        ...

    def get_par_réf_lot(self, réf_lot: str) -> model.Produit | None:
        """Recupere un produit contenant le lot de reference donnee."""
        ...
```

On remarque :

- **`add()` et `get()` travaillent avec des `Produit`**, jamais des `Lot`.
- **`get_par_réf_lot()`** retrouve le `Produit` parent à partir d'une référence de lot. Même ici, c'est l'agrégat entier qui est retourné.
- La méthode `seen` (un `set[Produit]`) permet de garder une trace de tous les agrégats chargés ou ajoutés, ce qui sera utile pour collecter les domain events.

---

## Choisir les frontières de l'Agrégat

Le choix des frontières est une décision de conception cruciale. La règle à retenir :

!!! warning "Un agrégat = une transaction = un verrou"
    Chaque agrégat délimite une **transaction**. Pendant qu'une transaction modifie un agrégat, aucune autre transaction ne peut le modifier. C'est ainsi qu'on garantit la cohérence.

### Trop gros : le problème de la contention

Si on faisait un agrégat unique `Entrepôt` contenant **tous** les produits et **tous** les lots, chaque allocation verrouillerait l'ensemble du stock. Deux commandes pour des produits différents ne pourraient pas être traitées en parallèle.

### Trop petit : le problème de l'incohérence

Si chaque `Lot` était son propre agrégat, on n'aurait aucun moyen de garantir que l'allocation choisit le bon lot. Deux transactions pourraient allouer le même lot simultanément sans le savoir.

### La bonne granularité : `Produit`

Un `Produit` regroupe tous les lots pour un SKU donné. C'est le bon compromis :

- **Cohérence** : toutes les règles d'allocation pour un SKU sont vérifiées dans une seule transaction.
- **Concurrence** : deux allocations pour des SKU différents se font en parallèle sans conflit.

```
+-------------------+   +-------------------+
|  Produit (SKU-A)  |   |  Produit (SKU-B)  |
|  +-------+        |   |  +-------+        |
|  | Lot 1 |        |   |  | Lot 3 |        |
|  +-------+        |   |  +-------+        |
|  +-------+        |   |  +-------+        |
|  | Lot 2 |        |   |  | Lot 4 |        |
|  +-------+        |   |  +-------+        |
+-------------------+   +-------------------+
  ^ transaction A        ^ transaction B
  (independantes)
```

---

## Le versioning optimiste (Optimistic Locking)

Même avec des frontières d'agrégat bien choisies, il reste un risque : deux transactions concurrentes peuvent tenter de modifier **le même** `Produit` simultanément. Le **numéro_version** résout ce problème :

1. Quand on charge un `Produit`, on lit son `numéro_version`.
2. Quand on le sauvegarde, on vérifie que le `numéro_version` en base n'a pas changé.
3. Si le numéro a changé (une autre transaction est passée entre-temps), la sauvegarde échoue.

```
Transaction A                       Transaction B
     |                                   |
     | SELECT ... => version = 3         |
     |                                   | SELECT ... => version = 3
     | allouer(...) => version = 4       |
     |                                   | allouer(...) => version = 4
     | UPDATE ... WHERE version=3        |
     | => OK (1 row)                     |
     |                                   | UPDATE ... WHERE version=3
     |                                   | => ECHEC (0 rows)
     v                                   v
```

La transaction B échoue car la clause `WHERE numéro_version=3` ne correspond plus à l'état en base. C'est le principe de l'**optimistic locking** : on suppose que les conflits sont rares, mais on les détecte au moment du `commit`.

Dans le mapping ORM (`src/allocation/adapters/orm.py`), la table `products` porte ce champ :

```python
products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sku", String(255)),
    Column("numero_version", Integer, nullable=False, server_default="0"),
)
```

Et dans la classe `Produit`, chaque appel à `allouer()` incrémente le compteur :

```python
lot.allouer(ligne)
self.numéro_version += 1
return lot.référence
```

!!! tip "Pourquoi optimiste ?"
    On parle de verrouillage **optimiste** parce qu'on ne pose pas de verrou explicite en base de données (pas de `SELECT ... FOR UPDATE`). On laisse les transactions avancer en parallèle et on ne détecte le conflit qu'au moment du `commit`. C'est plus performant quand les conflits sont rares.

---

## Les Domain Events émis par l'Agrégat

L'agrégat `Produit` ne se contente pas de modifier son état interne. Il **émet des événements** qui signalent ce qui s'est passé :

| Événement | Quand | Déclencheur |
|-----------|-------|-------------|
| `RuptureDeStock` | Aucun lot ne peut accueillir la ligne | `allouer()` |
| `Désalloué` | Une ligne est désallouée suite à un changement de quantité | `modifier_quantité_lot()` |

Ces événements sont collectés dans `self.événements` et seront publiés par l'infrastructure (le Unit of Work et le message bus) après la transaction. C'est une séparation nette entre "ce qui s'est passé" et "ce qu'il faut faire ensuite".

```python
# Dans Produit.__init__
self.événements: list[events.Event] = []

# Dans allouer(), si rupture de stock :
self.événements.append(events.RuptureDeStock(sku=ligne.sku))

# Dans modifier_quantité_lot(), pour chaque ligne désallouée :
self.événements.append(
    events.Désalloué(id_commande=ligne.id_commande, sku=ligne.sku, quantité=ligne.quantité)
)
```

---

## Schéma récapitulatif

```
+------------------------------------------------------------+
|                                                            |
|   Aggregate : Produit                                      |
|                                                            |
|   Identite :  sku = "BLUE-VASE"                            |
|   Version :   numéro_version = 3                           |
|   Events :    [RuptureDeStock(...), Désalloué(...)]        |
|                                                            |
|   +------------------------+  +------------------------+   |
|   |  Lot                   |  |  Lot                   |   |
|   |  référence: "lot-01"   |  |  référence: "lot-02"   |   |
|   |  sku: "BLUE-VASE"      |  |  sku: "BLUE-VASE"      |   |
|   |  quantité: 100         |  |  quantité: 50          |   |
|   |  eta: None (en stock)  |  |  eta: 2025-03-15       |   |
|   |                        |  |                        |   |
|   |  _allocations:         |  |  _allocations:         |   |
|   |    {LigneDeCommande(.),|  |    {LigneDeCommande(.)}|   |
|   |     LigneDeCommande(.)}|  |                        |   |
|   +------------------------+  +------------------------+   |
|                                                            |
+------------------------------------------------------------+
         ^
         |
    Aggregate Root : le seul point d'acces
    Repository.get("BLUE-VASE") -> Produit
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
| **Optimistic Locking** | Le `numéro_version` détecte les conflits entre transactions concurrentes. |
| **Repository** | Il travaille au niveau de l'agrégat, pas de ses composants internes. |
| **Domain Events** | L'agrégat émet des événements pour signaler ce qui s'est passé. |

## Exercices

!!! example "Exercice 1 -- Frontières alternatives"
    Imaginez que chaque `Lot` soit son propre agrégat (pas de `Produit` englobant). Quels invariants ne pourraient plus être garantis ? Que se passerait-il en cas d'accès concurrent ?

!!! example "Exercice 2 -- Verrouillage pessimiste"
    Remplacez l'optimistic locking par un `SELECT ... FOR UPDATE` dans le repository SQLAlchemy. Quels sont les avantages et inconvénients de chaque approche ?

!!! example "Exercice 3 -- Nouvel invariant"
    Ajoutez la règle métier : "un produit ne peut pas avoir plus de 10 lots actifs". Où cette règle doit-elle vivre ? Implémentez-la dans `Produit` et écrivez le test correspondant.

---

!!! quote "À retenir"
    L'agrégat est la réponse à la question : **"quels objets doivent être cohérents entre eux ?"**. Dans notre domaine, tous les lots d'un même produit doivent être cohérents, donc `Produit` est l'agrégat qui contient les `Lot`. Le `numéro_version` garantit qu'une seule transaction à la fois peut modifier un `Produit` donné.

---

*Chapitre suivant : [Events et le Message Bus](../partie2/chapitre_08_events.md) -- comment les événements émis par l'agrégat déclenchent des actions dans le reste du système.*
