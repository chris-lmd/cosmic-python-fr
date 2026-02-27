# Chapitre 4 -- La Service Layer

!!! abstract "Ce que vous allez apprendre"

    - Pourquoi la logique d'orchestration n'a pas sa place dans les routes Flask
    - Ce qu'est une Service Layer et ce qu'elle contient (et ne contient **pas**)
    - Comment ecrire des handlers fins et proceduraux
    - Comment transformer Flask en thin adapter qui ne fait que traduire HTTP
    - Comment tester l'orchestration sans framework web ni base de donnees

---

## Le probleme : des routes Flask qui grossissent

Dans les chapitres precedents, nous avons construit un modele de domaine (`Product`, `Batch`, `OrderLine`) et un Repository pour persister nos agregats. Imaginons maintenant une premiere route Flask pour allouer du stock :

```python
# Version naive -- toute la logique dans la route
@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    # 1. Ouvrir une session / transaction
    session = get_session()
    # 2. Recuperer l'agregat
    product = repo.get(data["sku"])
    if product is None:
        return jsonify({"message": "SKU inconnu"}), 400
    # 3. Construire le value object
    line = OrderLine(data["orderid"], data["sku"], data["qty"])
    # 4. Appeler la logique metier
    batchref = product.allocate(line)
    # 5. Committer
    session.commit()
    return jsonify({"batchref": batchref}), 201
```

Ce code fonctionne, mais il pose plusieurs problemes :

**Duplication.** Si demain on ajoute une CLI, un worker Celery ou un consumer Redis, il faudra recopier toute cette sequence (recuperer le produit, construire la ligne, allouer, committer). Chaque point d'entree reimplementera le meme workflow.

**Testabilite.** Pour tester cette logique, on doit demarrer Flask, envoyer de vraies requetes HTTP et souvent brancher une base de donnees. Les tests deviennent lents et fragiles.

**Responsabilite mal placee.** Flask est un framework de presentation. Son role est de convertir des requetes HTTP en appels applicatifs, pas d'orchestrer un workflow metier.

---

## La Service Layer : une couche d'orchestration

La Service Layer est une couche mince qui se place **entre** les points d'entree (Flask, CLI...) et le modele de domaine. Son role est precis :

1. Recuperer les objets necessaires via le Repository
2. Appeler les methodes du domaine
3. Committer la transaction

Elle **ne contient pas** de logique metier. La logique metier reste dans le modele de domaine (c'est `Product.allocate()` qui decide quel batch choisir, pas le handler). La Service Layer se contente de **coordonner**.

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
│          Modele de domaine                    │
│  (Product, Batch, OrderLine)                  │
│  Contient TOUTE la logique metier             │
└──────────────────────────────────────────────┘
```

---

## Les handlers : fins et proceduraux

Nos handlers vivent dans `src/allocation/service_layer/handlers.py`. Chaque handler prend une command (un simple dataclass decrivant l'intention) et un Unit of Work, puis orchestre le workflow en quelques lignes.

### `add_batch` -- creer un lot de stock

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

Le handler est **procedural** : il ouvre le Unit of Work, recupere ou cree le produit, ajoute le batch, puis committe. Pas de boucle complexe, pas de logique conditionnelle metier.

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

Observez que **toute la logique d'allocation** (trier les batches par ETA, verifier la quantite disponible, choisir le meilleur lot) est dans `product.allocate()`. Le handler ne fait que preparer les donnees et declencher l'appel.

### La ligne de demarcation

Un bon test pour savoir si la logique est au bon endroit : si vous enlevez le handler et appelez directement `product.allocate()` dans un test unitaire, la regle metier fonctionne-t-elle toujours ? Si oui, la logique est bien dans le domaine. Le handler ne fait que du "plumbing".

---

## Flask comme thin adapter

Maintenant que la Service Layer existe, Flask n'a plus qu'un seul role : **traduire le protocole HTTP** en objets que la couche service comprend, puis convertir le resultat en reponse HTTP.

Voici le code reel de `src/allocation/entrypoints/flask_app.py` :

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

Chaque endpoint suit la meme structure en trois temps :

1. **Extraire** les donnees de la requete HTTP (`request.json`)
2. **Construire** une command (un dataclass immuable)
3. **Deleguer** au bus (qui dispatch vers le handler)

Il n'y a **aucune logique metier** dans ces fonctions. Pas de `if` sur la disponibilite du stock, pas de tri des batches, pas d'acces direct au Repository. Flask ne sait meme pas que des batches existent.

!!! tip "Adaptateurs et ports"

    Flask est un **adapter** au sens de l'architecture hexagonale. Il adapte le port HTTP vers l'interface de la Service Layer. Si demain vous remplacez Flask par FastAPI, seul cet adaptateur change -- ni les handlers, ni le domaine ne sont touches.

---

## Testabilite : des fakes plutot que des mocks

L'un des gains majeurs de la Service Layer est la **testabilite**. On peut tester toute l'orchestration sans demarrer Flask et sans toucher a la base de donnees, en remplacant les adaptateurs concrets par des fakes.

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

Ces fakes sont des implementations **en memoire** des abstractions. Le `FakeRepository` stocke les produits dans un `set` Python au lieu de SQLAlchemy, et le `FakeUnitOfWork` trace les commits sans toucher a aucune base de donnees.

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

- Pas de `app.test_client()` -- aucune requete HTTP
- Pas de `session` SQLAlchemy -- aucune base de donnees
- Pas de `mock.patch` -- on injecte de vrais objets (les fakes)

Les tests sont rapides (millisecondes), isoles et lisibles. Ils verifient le **comportement metier** (est-ce que le batch est bien cree ? est-ce que l'allocation retourne la bonne reference ?) sans etre couples a aucune infrastructure.

### Et les tests de l'API ?

Les tests de l'API Flask deviennent des **tests d'integration legers** : ils verifient uniquement que Flask parse correctement le JSON, appelle le bon handler, et retourne le bon code HTTP. La logique metier, elle, est deja couverte par les tests unitaires des handlers.

---

## La pyramide des tests

Avec la Service Layer en place, la repartition des tests evolue :

| Couche | Type de test | Vitesse | Ce qu'on teste |
|--------|-------------|---------|----------------|
| Domaine | Unitaire | Tres rapide | Regles metier pures |
| Service Layer | Unitaire (avec fakes) | Rapide | Orchestration, workflows |
| Entrypoints | Integration | Plus lent | Traduction HTTP, serialisation |
| End-to-end | Systeme | Lent | Le systeme complet |

La majorite des tests se concentre sur les deux premieres couches. Les tests d'integration de l'API sont peu nombreux car ils ne verifient que le "cablage".

---

## Resume

La Service Layer est le ciment entre le monde exterieur et le modele de domaine. Elle applique le **principe de responsabilite unique** a l'echelle des couches :

| Couche | Responsabilite | Exemple |
|--------|---------------|---------|
| **Entrypoints** | Traduire un protocole externe en commands | Flask parse le JSON, construit `commands.Allocate`, delegue au bus |
| **Service Layer** | Orchestrer le workflow applicatif | Le handler ouvre le UoW, recupere le produit, appelle `product.allocate()`, committe |
| **Domaine** | Implementer les regles metier | `Product.allocate()` trie les batches, verifie la disponibilite, choisit le lot |

Quelques principes a retenir :

- **Les handlers sont fins.** Quelques lignes de code procedural. Si un handler depasse 15 lignes, de la logique metier s'est probablement glissee au mauvais endroit.
- **Le domaine ne sait rien de la persistance.** Il ne connait ni le Repository, ni le Unit of Work. C'est le handler qui fait le lien.
- **Les entrypoints ne savent rien du domaine.** Flask ne manipule jamais directement un `Product` ou un `Batch`. Il envoie des commands et recoit des resultats.
- **Les fakes sont preferes aux mocks.** En implementant les interfaces abstraites (`AbstractRepository`, `AbstractUnitOfWork`), on obtient des doubles de test fiables et maintenables.

!!! quote "Regle d'or"

    Si vous ne savez pas ou placer un bout de code, posez-vous la question : "Est-ce une **regle metier** (domaine), une **etape du workflow** (service layer), ou une **traduction de protocole** (entrypoint) ?"

---

*Prochain chapitre : [TDD a haute et basse vitesse](chapitre_05_tdd.md) -- comment exploiter cette architecture en couches pour ecrire des tests a la fois rapides et fiables.*
