# Chapitre 4 -- La Service Layer

!!! abstract "Ce que vous allez apprendre"

    - Pourquoi la logique d'orchestration n'a pas sa place dans les routes Flask
    - Ce qu'est une Service Layer et ce qu'elle contient (et ne contient **pas**)
    - Comment écrire des handlers fins et procéduraux
    - Comment transformer Flask en thin adapter qui ne fait que traduire HTTP
    - Comment tester l'orchestration sans framework web ni base de données

---

## Le problème : des routes Flask qui grossissent

Dans les chapitres précédents, nous avons construit un modèle de domaine (`Product`, `Batch`, `OrderLine`) et un Repository pour persister nos agrégats. Imaginons maintenant une première route Flask pour allouer du stock :

```python
# Version naïve -- toute la logique dans la route
@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    # 1. Ouvrir une session / transaction
    session = get_session()
    # 2. Récupérer l'agrégat
    product = repo.get(data["sku"])
    if product is None:
        return jsonify({"message": "SKU inconnu"}), 400
    # 3. Construire le value object
    line = OrderLine(data["orderid"], data["sku"], data["qty"])
    # 4. Appeler la logique métier
    batchref = product.allocate(line)
    # 5. Committer
    session.commit()
    return jsonify({"batchref": batchref}), 201
```

Ce code fonctionne, mais il pose plusieurs problèmes :

**Duplication.** Si demain on ajoute une CLI, un worker Celery ou un consumer Redis, il faudra recopier toute cette séquence (récupérer le produit, construire la ligne, allouer, committer). Chaque point d'entrée réimplémentera le même workflow.

**Testabilité.** Pour tester cette logique, on doit démarrer Flask, envoyer de vraies requêtes HTTP et souvent brancher une base de données. Les tests deviennent lents et fragiles.

**Responsabilité mal placée.** Flask est un framework de présentation. Son rôle est de convertir des requêtes HTTP en appels applicatifs, pas d'orchestrer un workflow métier.

---

## La Service Layer : une couche d'orchestration

La Service Layer est une couche mince qui se place **entre** les points d'entrée (Flask, CLI...) et le modèle de domaine. Son rôle est précis :

1. Récupérer les objets nécessaires via le Repository
2. Appeler les méthodes du domaine
3. Committer la transaction

Elle **ne contient pas** de logique métier. La logique métier reste dans le modèle de domaine (c'est `Product.allocate()` qui décide quel batch choisir, pas le handler). La Service Layer se contente de **coordonner**.

```
┌──────────────────────────────────────────────┐
│              Entrypoints                      │
│         (Flask, CLI, Consumer...)             │
│  Convertit le protocole externe en commands   │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│            Service Layer                      │
│         (handlers.py)                         │
│  Orchestre : UoW → Repository → Domaine       │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│          Modèle de domaine                    │
│  (Product, Batch, OrderLine)                  │
│  Contient TOUTE la logique métier             │
└──────────────────────────────────────────────┘
```

---

## Les handlers : fins et procéduraux

Nos handlers vivent dans `src/allocation/service_layer/handlers.py`. Chaque handler prend une command (un simple dataclass décrivant l'intention) et un Unit of Work, puis orchestre le workflow en quelques lignes.

### `add_batch` -- créer un lot de stock

```python
def add_batch(
    cmd: commands.CreateBatch,
    uow: AbstractUnitOfWork,
) -> None:
    with uow:
        product = uow.products.get(sku=cmd.sku)
        if product is None:
            product = model.Product(sku=cmd.sku, batches=[])
            uow.products.add(product)
        product.batches.append(
            model.Batch(ref=cmd.ref, sku=cmd.sku, qty=cmd.qty, eta=cmd.eta)
        )
        uow.commit()
```

Le handler est **procédural** : il ouvre le Unit of Work, récupère ou crée le produit, ajoute le batch, puis committe. Pas de boucle complexe, pas de logique conditionnelle métier.

### `allocate` -- allouer une ligne de commande

```python
def allocate(
    cmd: commands.Allocate,
    uow: AbstractUnitOfWork,
) -> str:
    line = model.OrderLine(orderid=cmd.orderid, sku=cmd.sku, qty=cmd.qty)
    with uow:
        product = uow.products.get(sku=cmd.sku)
        if product is None:
            raise InvalidSku(f"SKU inconnu : {cmd.sku}")
        batchref = product.allocate(line)
        uow.commit()
    return batchref
```

Observez que **toute la logique d'allocation** (trier les batches par ETA, vérifier la quantité disponible, choisir le meilleur lot) est dans `product.allocate()`. Le handler ne fait que préparer les données et déclencher l'appel.

### La ligne de démarcation

Un bon test pour savoir si la logique est au bon endroit : si vous enlevez le handler et appelez directement `product.allocate()` dans un test unitaire, la règle métier fonctionne-t-elle toujours ? Si oui, la logique est bien dans le domaine. Le handler ne fait que du "plumbing".

---

## Flask comme thin adapter

Maintenant que la Service Layer existe, Flask n'a plus qu'un seul rôle : **traduire le protocole HTTP** en objets que la couche service comprend, puis convertir le résultat en réponse HTTP.

Voici le code réel de `src/allocation/entrypoints/flask_app.py` :

```python
app = Flask(__name__)
bus = bootstrap.bootstrap()


@app.route("/add_batch", methods=["POST"])
def add_batch_endpoint():
    data = request.json
    eta = data.get("eta")
    if eta is not None:
        eta = datetime.fromisoformat(eta).date()

    cmd = commands.CreateBatch(
        ref=data["ref"],
        sku=data["sku"],
        qty=data["qty"],
        eta=eta,
    )
    bus.handle(cmd)
    return "OK", 201


@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    try:
        cmd = commands.Allocate(
            orderid=data["orderid"],
            sku=data["sku"],
            qty=data["qty"],
        )
        results = bus.handle(cmd)
        batchref = results.pop(0)
    except handlers.InvalidSku as e:
        return jsonify({"message": str(e)}), 400

    return jsonify({"batchref": batchref}), 201
```

Chaque endpoint suit la même structure en trois temps :

1. **Extraire** les données de la requête HTTP (`request.json`)
2. **Construire** une command (un dataclass immuable)
3. **Déléguer** au bus (qui dispatch vers le handler)

Il n'y a **aucune logique métier** dans ces fonctions. Pas de `if` sur la disponibilité du stock, pas de tri des batches, pas d'accès direct au Repository. Flask ne sait même pas que des batches existent.

!!! tip "Adaptateurs et ports"

    Flask est un **adapter** au sens de l'architecture hexagonale. Il adapte le port HTTP vers l'interface de la Service Layer. Si demain vous remplacez Flask par FastAPI, seul cet adaptateur change -- ni les handlers, ni le domaine ne sont touchés.

---

## Testabilité : des fakes plutôt que des mocks

L'un des gains majeurs de la Service Layer est la **testabilité**. On peut tester toute l'orchestration sans démarrer Flask et sans toucher à la base de données, en remplaçant les adaptateurs concrets par des fakes.

### FakeRepository et FakeUnitOfWork

```python
class FakeRepository(AbstractRepository):
    def __init__(self, products: list[model.Product] | None = None):
        super().__init__()
        self._products = set(products or [])

    def _add(self, product: model.Product) -> None:
        self._products.add(product)

    def _get(self, sku: str) -> model.Product | None:
        return next((p for p in self._products if p.sku == sku), None)

    def _get_by_batchref(self, batchref: str) -> model.Product | None:
        return next(
            (p for p in self._products for b in p.batches
             if b.reference == batchref),
            None,
        )


class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    def __init__(self):
        self.products = FakeRepository([])
        self.committed = False

    def __enter__(self):
        return super().__enter__()

    def __exit__(self, *args):
        pass

    def _commit(self):
        self.committed = True

    def rollback(self):
        pass
```

Ces fakes sont des implémentations **en mémoire** des abstractions. Le `FakeRepository` stocke les produits dans un `set` Python au lieu de SQLAlchemy, et le `FakeUnitOfWork` trace les commits sans toucher à aucune base de données.

### Les tests des handlers

Avec ces fakes, tester un handler est direct et rapide :

```python
class TestAddBatch:
    def test_add_batch_for_new_product(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "COUSSIN-CARRE", 100, None))

        assert bus.uow.products.get("COUSSIN-CARRE") is not None
        assert bus.uow.committed

    def test_add_batch_for_existing_product(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "LAMPE-RONDE", 100, None))
        bus.handle(commands.CreateBatch("b2", "LAMPE-RONDE", 99, None))

        product = bus.uow.products.get("LAMPE-RONDE")
        assert len(product.batches) == 2


class TestAllocate:
    def test_allocate_returns_batch_ref(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "CHAISE-COMFY", 100, None))
        results = bus.handle(commands.Allocate("o1", "CHAISE-COMFY", 10))

        assert results.pop(0) == "b1"

    def test_allocate_errors_for_invalid_sku(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CreateBatch("b1", "VRAI-SKU", 100, None))

        with pytest.raises(handlers.InvalidSku, match="SKU-INEXISTANT"):
            bus.handle(commands.Allocate("o1", "SKU-INEXISTANT", 10))
```

Remarquez ce que ces tests **ne font pas** :

- Pas de `app.test_client()` -- aucune requête HTTP
- Pas de `session` SQLAlchemy -- aucune base de données
- Pas de `mock.patch` -- on injecte de vrais objets (les fakes)

Les tests sont rapides (millisecondes), isolés et lisibles. Ils vérifient le **comportement métier** (est-ce que le batch est bien créé ? est-ce que l'allocation retourne la bonne référence ?) sans être couplés à aucune infrastructure.

### Et les tests de l'API ?

Les tests de l'API Flask deviennent des **tests d'intégration légers** : ils vérifient uniquement que Flask parse correctement le JSON, appelle le bon handler, et retourne le bon code HTTP. La logique métier, elle, est déjà couverte par les tests unitaires des handlers.

---

## La pyramide des tests

Avec la Service Layer en place, la répartition des tests évolue :

| Couche | Type de test | Vitesse | Ce qu'on teste |
|--------|-------------|---------|----------------|
| Domaine | Unitaire | Très rapide | Règles métier pures |
| Service Layer | Unitaire (avec fakes) | Rapide | Orchestration, workflows |
| Entrypoints | Intégration | Plus lent | Traduction HTTP, sérialisation |
| End-to-end | Système | Lent | Le système complet |

La majorité des tests se concentre sur les deux premières couches. Les tests d'intégration de l'API sont peu nombreux car ils ne vérifient que le "câblage".

---

## Résumé

La Service Layer est le ciment entre le monde extérieur et le modèle de domaine. Elle applique le **principe de responsabilité unique** à l'échelle des couches :

| Couche | Responsabilité | Exemple |
|--------|---------------|---------|
| **Entrypoints** | Traduire un protocole externe en commands | Flask parse le JSON, construit `commands.Allocate`, délègue au bus |
| **Service Layer** | Orchestrer le workflow applicatif | Le handler ouvre le UoW, récupère le produit, appelle `product.allocate()`, committe |
| **Domaine** | Implémenter les règles métier | `Product.allocate()` trie les batches, vérifie la disponibilité, choisit le lot |

Quelques principes à retenir :

- **Les handlers sont fins.** Quelques lignes de code procédural. Si un handler dépasse 15 lignes, de la logique métier s'est probablement glissée au mauvais endroit.
- **Le domaine ne sait rien de la persistance.** Il ne connaît ni le Repository, ni le Unit of Work. C'est le handler qui fait le lien.
- **Les entrypoints ne savent rien du domaine.** Flask ne manipule jamais directement un `Product` ou un `Batch`. Il envoie des commands et reçoit des résultats.
- **Les fakes sont préférés aux mocks.** En implémentant les interfaces abstraites (`AbstractRepository`, `AbstractUnitOfWork`), on obtient des doubles de test fiables et maintenables.

!!! quote "Règle d'or"

    Si vous ne savez pas où placer un bout de code, posez-vous la question : "Est-ce une **règle métier** (domaine), une **étape du workflow** (service layer), ou une **traduction de protocole** (entrypoint) ?"

---

*Prochain chapitre : [TDD à haute et basse vitesse](chapitre_05_tdd.md) -- comment exploiter cette architecture en couches pour écrire des tests à la fois rapides et fiables.*
