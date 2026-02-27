# Chapitre 1 -- Le Domain Model

## Pourquoi un modèle de domaine ?

Imaginons un système d'allocation de stock. Un client passe une commande, et le système doit décider quel lot de marchandise utiliser pour honorer cette commande. Simple en apparence, mais les règles s'accumulent vite : on préfère puiser dans le stock déjà en entrepôt plutôt que dans une livraison à venir, on choisit la livraison la plus proche si tout le stock en entrepôt est épuisé, on ne peut pas allouer plus que ce qui est disponible, on ne peut pas allouer un SKU différent de celui commandé...

Dans beaucoup de projets, cette logique finit dispersée un peu partout : dans les vues Django, dans les endpoints FastAPI, dans des scripts SQL. C'est ce qu'on appelle parfois un **transaction script** -- un gros bloc procédural qui mélange logique métier, accès aux données et orchestration. Ça fonctionne au début, puis ça devient un cauchemar à maintenir et à tester.

Le **Domain Model** est une réponse à ce problème. L'idée est de concentrer toute la logique métier dans une couche de code pur Python, sans aucune dépendance technique. Pas de base de données, pas de framework web, pas d'import `requests` ou `sqlalchemy`. Juste des classes, des méthodes et des règles métier.

!!! tip "L'avantage principal"
    Un modèle de domaine pur se teste avec de simples tests unitaires, sans fixtures de base de données ni serveur HTTP. Les tests s'exécutent en millisecondes.

## Qu'est-ce qu'un Domain Model ?

Le Domain Model est une représentation en code des concepts, des règles et des processus du domaine métier. C'est la traduction directe de ce que les experts métier décrivent quand ils parlent de leur travail.

Dans notre cas, les experts métier parlent de **lignes de commande**, de **lots de stock**, de **SKU** (Stock Keeping Unit), d'**allocation** et de **quantité disponible**. Le Domain Model reprend exactement ce vocabulaire.

```
Vocabulaire metier          Code
-----------------          ----
Ligne de commande    -->   LigneDeCommande
Lot de stock         -->   Lot
Allouer              -->   allouer()
Quantite disponible  -->   quantité_disponible
Reference produit    -->   SKU (str)
```

La distinction fondamentale avec un transaction script, c'est l'endroit où vivent les règles. Dans un transaction script, la logique est dans le handler :

```python
# Transaction script -- a eviter
def allouer(id_commande, sku, quantité, session):
    lots = session.query(Lot).filter_by(sku=sku).all()
    lots.sort(key=lambda l: (l.eta is not None, l.eta))
    for lot in lots:
        if lot._quantité_achetée - lot.quantité_allouée >= quantité:
            lot.quantité_allouée += quantité
            session.commit()
            return lot.référence
    raise RuptureDeStock(sku)
```

Dans un Domain Model, la logique vit dans les objets du domaine eux-mêmes. Le handler ne fait que les orchestrer. C'est cette séparation qui rend le code testable, lisible et maintenable.

## Value Objects

Un **Value Object** est un objet défini par ses attributs, pas par une identité. Deux billets de 10 euros sont interchangeables : peu importe lequel vous avez, ce qui compte c'est la valeur. De la même façon, deux lignes de commande avec le même `id_commande`, le même `sku` et la même `quantité` sont identiques.

Voici notre Value Object `LigneDeCommande` :

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class LigneDeCommande:
    """
    Value Object représentant une ligne de commande.

    Un value object est immuable et défini par ses attributs,
    pas par une identité. Deux LigneDeCommande avec les mêmes
    attributs sont considérées comme identiques.
    """

    id_commande: str
    sku: str
    quantité: int
```

Le décorateur `@dataclass(frozen=True)` fait deux choses essentielles :

1. **Immutabilité** -- On ne peut pas modifier les attributs après création. Un `ligne.quantité = 5` lèvera une `FrozenInstanceError`. C'est voulu : un Value Object ne change pas, on en crée un nouveau si besoin.

2. **Hashabilité** -- Un objet `frozen` est automatiquement hashable, ce qui permet de l'utiliser dans des `set` et comme clé de `dict`. C'est indispensable pour notre modèle, car `Lot` stocke ses allocations dans un `set[LigneDeCommande]`.

??? note "Pourquoi `@dataclass` et pas `NamedTuple` ?"
    Les deux sont des choix valables. `@dataclass(frozen=True)` offre un peu plus de flexibilité (héritage, méthodes, valeurs par défaut mutables via `field`). `NamedTuple` est légèrement plus performant en mémoire. Pour un Domain Model, la différence est négligeable. L'important, c'est l'immutabilité et l'égalité structurelle.

On peut vérifier le comportement d'égalité :

```python
def test_equality():
    """Deux LigneDeCommande avec les mêmes attributs sont égales (value object)."""
    ligne1 = LigneDeCommande("commande1", "SKU-001", 10)
    ligne2 = LigneDeCommande("commande1", "SKU-001", 10)
    assert ligne1 == ligne2

def test_inequality():
    ligne1 = LigneDeCommande("commande1", "SKU-001", 10)
    ligne2 = LigneDeCommande("commande2", "SKU-001", 10)
    assert ligne1 != ligne2
```

Pas besoin d'écrire `__eq__` : `@dataclass` le génère automatiquement en comparant tous les attributs.

## Entities

Une **Entity** est un objet avec une identité qui persiste dans le temps. Même si ses attributs changent, l'entité reste la même. Un lot de stock avec la référence `"lot-042"` reste le même lot, qu'il contienne 100 ou 50 unités.

Voici notre entité `Lot` :

```python
class Lot:
    """
    Entité représentant un lot de stock.

    Un Lot a une identité (sa référence) et un cycle de vie.
    Il contient une quantité de stock pour un SKU donné,
    avec une date d'arrivée (ETA) optionnelle.
    """

    def __init__(self, réf: str, sku: str, quantité: int, eta: Optional[date] = None):
        self.référence = réf
        self.sku = sku
        self.eta = eta
        self._quantité_achetée = quantité
        self._allocations: set[LigneDeCommande] = set()

    def __repr__(self) -> str:
        return f"<Lot {self.référence}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Lot):
            return NotImplemented
        return self.référence == other.référence

    def __hash__(self) -> int:
        return hash(self.référence)
```

Trois points importants :

**`__eq__` compare uniquement la référence.** Deux objets `Lot` avec la même référence sont considérés comme la même entité, peu importe les autres attributs. C'est la définition même d'une entité : l'égalité est basée sur l'identité, pas sur la valeur.

**`__hash__` est basé sur la référence.** Quand on redéfinit `__eq__`, Python rend l'objet non-hashable par défaut. On doit donc redéfinir `__hash__` explicitement, en se basant sur le même attribut que `__eq__`.

**`NotImplemented` plutôt que `False`.** Quand on compare un `Lot` avec un objet d'un autre type, on retourne `NotImplemented` pour laisser Python essayer l'autre opérande. C'est une bonne pratique souvent oubliée.

!!! warning "Entity vs Value Object : la règle"
    Si deux objets avec les mêmes attributs sont interchangeables, c'est un Value Object. Si un objet a un cycle de vie et une identité qui persiste même quand ses attributs changent, c'est une Entity.

## Les règles métier dans le domaine

C'est ici que le Domain Model prend tout son sens. Les règles métier ne sont pas dans un service externe ou dans un handler : elles vivent directement dans les objets du domaine.

### Vérifier qu'on peut allouer

```python
def peut_allouer(self, ligne: LigneDeCommande) -> bool:
    """Vérifie si ce lot peut accueillir la ligne de commande."""
    return self.sku == ligne.sku and self.quantité_disponible >= ligne.quantité
```

Deux conditions, et elles se lisent comme du langage naturel : le SKU doit correspondre, et la quantité disponible doit être suffisante.

### Allouer une ligne de commande

```python
def allouer(self, ligne: LigneDeCommande) -> None:
    """Alloue une ligne de commande à ce lot."""
    if self.peut_allouer(ligne):
        self._allocations.add(ligne)
```

L'allocation revient à ajouter la ligne de commande dans l'ensemble `_allocations`. Comme `LigneDeCommande` est un Value Object hashable, le `set` garantit l'idempotence : allouer deux fois la même ligne n'a aucun effet.

### Désallouer

```python
def désallouer(self, ligne: LigneDeCommande) -> None:
    """Désalloue une ligne de commande de ce lot."""
    if ligne in self._allocations:
        self._allocations.discard(ligne)
```

La désallocation est l'opération inverse. On utilise `discard` plutôt que `remove` pour éviter une exception si la ligne n'est pas présente, mais la vérification `if ligne in self._allocations` rend l'intention explicite.

### Quantités calculées

```python
@property
def quantité_allouée(self) -> int:
    return sum(ligne.quantité for ligne in self._allocations)

@property
def quantité_disponible(self) -> int:
    return self._quantité_achetée - self.quantité_allouée
```

La quantité disponible est toujours calculée à partir de l'état réel des allocations. Pas de compteur à maintenir manuellement, pas de risque de désynchronisation. C'est un choix de conception délibéré : on préfère recalculer plutôt que de maintenir un état dérivé.

??? note "Performance"
    Recalculer `quantité_disponible` à chaque accès peut sembler coûteux. En pratique, un lot a rarement plus de quelques dizaines d'allocations. Si la performance devenait un problème, on pourrait ajouter un cache -- mais pas avant d'avoir mesuré. L'optimisation prématurée est l'ennemi du code clair.

## La stratégie d'allocation

Quand un client passe une commande, on veut allouer depuis le lot le plus pertinent. La règle métier est :

1. **D'abord les lots en stock** (ceux qui sont déjà en entrepôt, sans ETA).
2. **Puis les livraisons par ETA croissante** (la plus proche d'abord).

Pour implémenter cette stratégie, on définit `__gt__` sur `Lot` :

```python
def __gt__(self, other: Lot) -> bool:
    if self.eta is None:
        return False
    if other.eta is None:
        return True
    return self.eta > other.eta
```

La logique est la suivante :

- Un lot **sans ETA** (en stock) n'est jamais "plus grand" qu'un autre. Il sera donc toujours trié en premier.
- Un lot **avec ETA** est toujours "plus grand" qu'un lot sans ETA.
- Entre deux lots avec ETA, le tri se fait par date.

Cela permet d'utiliser simplement `sorted()` pour obtenir les lots dans l'ordre de préférence :

```python
class Produit:
    def allouer(self, ligne: LigneDeCommande) -> str:
        """
        Alloue une ligne de commande au lot le plus approprié.

        La stratégie d'allocation privilégie les lots en stock
        (sans ETA) puis les lots avec l'ETA la plus proche.
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

`sorted(self.lots)` trie les lots grâce à `__gt__`. Puis on prend le premier qui peut accueillir la ligne (`peut_allouer`). Si aucun lot ne convient, on émet un événement `RuptureDeStock`.

!!! tip "Pourquoi `__gt__` et pas `__lt__` ?"
    Python a besoin d'un seul opérateur de comparaison pour que `sorted()` fonctionne. On aurait pu définir `__lt__` à la place, avec la logique inversée. Le choix de `__gt__` est une convention : on considère que les lots les "plus grands" sont ceux qui arrivent le plus tard, ce qui est naturel quand on pense aux dates.

## Tester le modèle de domaine

L'avantage majeur d'un Domain Model pur, c'est la testabilité. Les tests sont simples, rapides et ne nécessitent aucune infrastructure.

### Tests du Lot

```python
from datetime import date, timedelta
from allocation.domain.model import Lot, LigneDeCommande, Produit


def make_lot_et_ligne(
    sku: str, quantité_lot: int, quantité_ligne: int
) -> tuple[Lot, LigneDeCommande]:
    return (
        Lot("lot-001", sku, quantité_lot, eta=date.today()),
        LigneDeCommande("ref-commande", sku, quantité_ligne),
    )


class TestLot:
    def test_allouer_reduit_quantite_disponible(self):
        lot, ligne = make_lot_et_ligne("PETITE-TABLE", 20, 2)
        lot.allouer(ligne)
        assert lot.quantité_disponible == 18

    def test_peut_allouer_si_disponible_superieur_au_requis(self):
        lot, ligne = make_lot_et_ligne("ELEGANTE-LAMPE", 20, 2)
        assert lot.peut_allouer(ligne)

    def test_ne_peut_pas_allouer_si_disponible_inferieur_au_requis(self):
        lot, ligne = make_lot_et_ligne("ELEGANTE-LAMPE", 2, 20)
        assert not lot.peut_allouer(ligne)

    def test_ne_peut_pas_allouer_si_skus_differents(self):
        lot = Lot("lot-001", "CHAISE-INCOMFORTABLE", 100, eta=None)
        ligne = LigneDeCommande("ref-commande", "COUSSIN-MOELLEUX", 10)
        assert not lot.peut_allouer(ligne)

    def test_allocation_est_idempotente(self):
        lot, ligne = make_lot_et_ligne("ANGULAR-DESK", 20, 2)
        lot.allouer(ligne)
        lot.allouer(ligne)
        assert lot.quantité_disponible == 18

    def test_desallouer(self):
        lot, ligne = make_lot_et_ligne("ANGULAR-DESK", 20, 2)
        lot.allouer(ligne)
        lot.désallouer(ligne)
        assert lot.quantité_disponible == 20
```

Remarquez la structure : chaque test crée ses objets, exécute une action et vérifie le résultat. Pas de `setUp` complexe, pas de mock, pas de base de données. Les noms des tests décrivent le comportement attendu en langage naturel.

### Tests de la stratégie d'allocation

```python
class TestProduit:
    def test_prefere_lots_en_stock_aux_livraisons(self):
        """Les lots en stock (sans ETA) sont préférés aux livraisons."""
        lot_en_stock = Lot("lot-en-stock", "HORLOGE-RETRO", 100, eta=None)
        lot_en_livraison = Lot(
            "lot-en-livraison", "HORLOGE-RETRO", 100,
            eta=date.today() + timedelta(days=1)
        )
        produit = Produit(
            sku="HORLOGE-RETRO",
            lots=[lot_en_stock, lot_en_livraison]
        )
        ligne = LigneDeCommande("réf-cmd", "HORLOGE-RETRO", 10)

        produit.allouer(ligne)

        assert lot_en_stock.quantité_disponible == 90
        assert lot_en_livraison.quantité_disponible == 100

    def test_prefere_lots_plus_proches(self):
        """Parmi les livraisons, on préfère la plus proche."""
        le_plus_tot = Lot("lot-rapide", "LAMPE-MINIMALE", 100, eta=date.today())
        moyen = Lot(
            "lot-normal", "LAMPE-MINIMALE", 100,
            eta=date.today() + timedelta(days=5)
        )
        le_plus_tard = Lot(
            "lot-lent", "LAMPE-MINIMALE", 100,
            eta=date.today() + timedelta(days=10)
        )
        produit = Produit(
            sku="LAMPE-MINIMALE",
            lots=[moyen, le_plus_tot, le_plus_tard]
        )
        ligne = LigneDeCommande("commande1", "LAMPE-MINIMALE", 10)

        produit.allouer(ligne)

        assert le_plus_tot.quantité_disponible == 90
        assert moyen.quantité_disponible == 100
        assert le_plus_tard.quantité_disponible == 100
```

Le test `test_prefere_lots_en_stock_aux_livraisons` passe les lots dans l'ordre inverse (le lot en livraison avant celui en stock) pour vérifier que le tri fonctionne. Le test `test_prefere_lots_plus_proches` mélange volontairement l'ordre (`moyen, le_plus_tot, le_plus_tard`) pour la même raison.

Ces tests s'exécutent en quelques millisecondes. On peut en avoir des centaines sans que la suite de tests ne ralentisse. C'est un avantage considérable par rapport aux tests d'intégration qui nécessitent une base de données.

## Résumé

### Les concepts clés

| Concept | Description | Exemple |
|---------|-------------|---------|
| **Domain Model** | Couche de code pur qui représente les règles métier, sans dépendance technique. | Le module `model.py` |
| **Value Object** | Objet défini par ses attributs, immuable, sans identité propre. | `LigneDeCommande` |
| **Entity** | Objet avec une identité persistante, même si ses attributs changent. | `Lot` |
| **Aggregate** | Entité racine qui garantit la cohérence d'un groupe d'objets. | `Produit` |

### Avantages du pattern

- **Testabilité** -- La logique métier se teste en isolation, sans infrastructure. Les tests sont rapides et fiables.
- **Lisibilité** -- Le code du domaine utilise le vocabulaire métier. Un expert non-technique peut le relire et vérifier les règles.
- **Maintenabilité** -- Les règles métier sont centralisées. Quand une règle change, on sait exactement où intervenir.
- **Indépendance technique** -- Le domaine ne dépend pas de la base de données ni du framework web. On peut changer d'ORM ou de framework sans toucher aux règles métier.

### Inconvénients du pattern

- **Complexité initiale** -- Pour des CRUD simples, un Domain Model est excessif. Un transaction script suffit.
- **Mapping objet-relationnel** -- Le domaine étant découplé de la persistance, il faut une couche de mapping (c'est le sujet du prochain chapitre sur le Repository pattern).
- **Courbe d'apprentissage** -- Les concepts de DDD (Entity, Value Object, Aggregate) demandent un investissement initial.

!!! tip "Quand utiliser ce pattern ?"
    Le Domain Model vaut l'investissement quand la logique métier est complexe et susceptible d'évoluer. Si votre application est essentiellement un CRUD avec peu de règles métier, un transaction script ou un framework comme Django avec ses modèles "fat" sera plus adapté. Il n'y a pas de honte à choisir la simplicité quand elle suffit.

---

*Dans le prochain chapitre, nous verrons comment persister ce modèle de domaine sans le contaminer avec des détails techniques, grâce au pattern **Repository**.*
