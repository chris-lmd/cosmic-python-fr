# Chapitre 4 -- La Service Layer

!!! info "Avant / Après"

    | | |
    |---|---|
    | **Avant** | Toute la logique dans les routes Flask |
    | **Après** | Handlers fins orchestrent, Flask ne fait que traduire HTTP |

!!! abstract "Ce que vous allez apprendre"

    - Pourquoi la logique d'orchestration n'a pas sa place dans les routes Flask
    - Ce qu'est une Service Layer et ce qu'elle contient (et ne contient **pas**)
    - Comment écrire des handlers fins et procéduraux
    - Comment transformer Flask en thin adapter qui ne fait que traduire HTTP
    - Comment tester l'orchestration sans framework web ni base de données

---

## Le problème : des routes Flask qui grossissent

Dans les chapitres précédents, nous avons construit un modèle de domaine (`Lot`, `LigneDeCommande`, `allouer()`) et un Repository pour persister nos objets dans un conteneur `Produit`. La logique d'allocation que nous avions écrite comme fonction libre au chapitre 1 est maintenant une **méthode** de `Produit` -- c'est lui qui possède les lots et qui sait comment les trier. Imaginons maintenant une première route Flask pour allouer du stock :

```python
# Version naïve -- toute la logique dans la route
@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    # 1. Ouvrir une session / transaction
    session = get_session()
    # 2. Récupérer l'agrégat
    produit = repo.get(data["sku"])
    if produit is None:
        return jsonify({"message": "SKU inconnu"}), 400
    # 3. Construire le value object
    ligne = LigneDeCommande(data["id_commande"], data["sku"], data["quantité"])
    # 4. Appeler la logique métier
    réf_lot = produit.allouer(ligne)
    # 5. Committer
    session.commit()
    return jsonify({"réf_lot": réf_lot}), 201
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

Elle **ne contient pas** de logique métier. La logique métier reste dans le modèle de domaine (c'est `Produit.allouer()` qui décide quel lot choisir, pas le handler). La Service Layer se contente de **coordonner**.

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
│  (Produit, Lot, LigneDeCommande)              │
│  Contient TOUTE la logique métier             │
└──────────────────────────────────────────────┘
```

---

## Les handlers : fins et procéduraux

Nos handlers vivent dans `src/allocation/service_layer/handlers.py`. Chaque handler prend une command (un simple dataclass décrivant l'intention) et un Unit of Work, puis orchestre le workflow en quelques lignes.

### `ajouter_lot` -- créer un lot de stock

```python
def ajouter_lot(
    cmd: commands.CréerLot,
    uow: AbstractUnitOfWork,
) -> None:
    with uow:
        produit = uow.produits.get(sku=cmd.sku)
        if produit is None:
            produit = model.Produit(sku=cmd.sku, lots=[])
            uow.produits.add(produit)
        produit.lots.append(
            model.Lot(réf=cmd.réf, sku=cmd.sku, quantité=cmd.quantité, eta=cmd.eta)
        )
        uow.commit()
```

Le handler est **procédural** : il ouvre le Unit of Work, récupère ou crée le produit, ajoute le lot, puis committe. Pas de boucle complexe, pas de logique conditionnelle métier.

### `allouer` -- allouer une ligne de commande

```python
def allouer(
    cmd: commands.Allouer,
    uow: AbstractUnitOfWork,
) -> str:
    ligne = model.LigneDeCommande(
        id_commande=cmd.id_commande, sku=cmd.sku, quantité=cmd.quantité
    )
    with uow:
        produit = uow.produits.get(sku=cmd.sku)
        if produit is None:
            raise SkuInconnu(f"SKU inconnu : {cmd.sku}")
        réf_lot = produit.allouer(ligne)
        uow.commit()
    return réf_lot
```

Observez que **toute la logique d'allocation** (trier les lots par ETA, vérifier la quantité disponible, choisir le meilleur lot) est dans `produit.allouer()`. Le handler ne fait que préparer les données et déclencher l'appel.

### La ligne de démarcation

Un bon test pour savoir si la logique est au bon endroit : si vous enlevez le handler et appelez directement `produit.allouer()` dans un test unitaire, la règle métier fonctionne-t-elle toujours ? Si oui, la logique est bien dans le domaine. Le handler ne fait que du "plumbing".

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

    cmd = commands.CréerLot(
        réf=data["ref"],
        sku=data["sku"],
        quantité=data["qty"],
        eta=eta,
    )
    bus.handle(cmd)
    return "OK", 201


@app.route("/allocate", methods=["POST"])
def allocate_endpoint():
    data = request.json
    try:
        cmd = commands.Allouer(
            id_commande=data["orderid"],
            sku=data["sku"],
            quantité=data["qty"],
        )
        results = bus.handle(cmd)
        réf_lot = results.pop(0)
    except handlers.SkuInconnu as e:
        return jsonify({"message": str(e)}), 400

    return jsonify({"batchref": réf_lot}), 201
```

Chaque endpoint suit la même structure en trois temps :

1. **Extraire** les données de la requête HTTP (`request.json`)
2. **Construire** une command (un dataclass immuable)
3. **Déléguer** au bus (qui dispatch vers le handler)

Il n'y a **aucune logique métier** dans ces fonctions. Pas de `if` sur la disponibilité du stock, pas de tri des lots, pas d'accès direct au Repository. Flask ne sait même pas que des lots existent.

!!! tip "Adaptateurs et ports"

    Flask est un **adapter** au sens de l'architecture hexagonale. Il adapte le port HTTP vers l'interface de la Service Layer. Si demain vous remplacez Flask par FastAPI, seul cet adaptateur change -- ni les handlers, ni le domaine ne sont touchés.

---

## Testabilité : des fakes plutôt que des mocks

L'un des gains majeurs de la Service Layer est la **testabilité**. On peut tester toute l'orchestration sans démarrer Flask et sans toucher à la base de données, en remplaçant les adaptateurs concrets par des fakes.

### FakeRepository et FakeUnitOfWork

On utilise le `FakeRepository` défini au [chapitre 2](chapitre_02_repository.md) et un `FakeUnitOfWork` qui l'encapsule (détaillé au [chapitre 6](chapitre_06_unit_of_work.md)). Ces fakes sont des implémentations **en mémoire** des abstractions : le `FakeRepository` stocke les produits dans un `set` Python, et le `FakeUnitOfWork` trace les commits via un booléen `self.committed` sans toucher à aucune base de données.

### Les tests des handlers

Avec ces fakes, tester un handler est direct et rapide :

```python
class TestAjouterLot:
    def test_ajouter_lot_pour_nouveau_produit(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("l1", "COUSSIN-CARRE", 100, None))

        assert bus.uow.produits.get("COUSSIN-CARRE") is not None
        assert bus.uow.committed

    def test_ajouter_lot_pour_produit_existant(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("l1", "LAMPE-RONDE", 100, None))
        bus.handle(commands.CréerLot("l2", "LAMPE-RONDE", 99, None))

        produit = bus.uow.produits.get("LAMPE-RONDE")
        assert len(produit.lots) == 2


class TestAllouer:
    def test_allouer_retourne_ref_lot(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("l1", "CHAISE-COMFY", 100, None))
        results = bus.handle(commands.Allouer("c1", "CHAISE-COMFY", 10))

        assert results.pop(0) == "l1"

    def test_allouer_erreur_pour_sku_inconnu(self):
        bus = bootstrap_test_bus()
        bus.handle(commands.CréerLot("l1", "VRAI-SKU", 100, None))

        with pytest.raises(handlers.SkuInconnu, match="SKU-INEXISTANT"):
            bus.handle(commands.Allouer("c1", "SKU-INEXISTANT", 10))
```

Remarquez ce que ces tests **ne font pas** :

- Pas de `app.test_client()` -- aucune requête HTTP
- Pas de `session` SQLAlchemy -- aucune base de données
- Pas de `mock.patch` -- on injecte de vrais objets (les fakes)

Les tests sont rapides (millisecondes), isolés et lisibles. Ils vérifient le **comportement métier** (est-ce que le lot est bien créé ? est-ce que l'allocation retourne la bonne référence ?) sans être couplés à aucune infrastructure.

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

## Exercices

!!! example "Exercice 1 -- Nouvelle command"
    Ajoutez un handler `désallouer` qui prend une command `Désallouer(id_commande, sku, quantité)` et retire une allocation. Écrivez le test correspondant avec le `FakeUnitOfWork`. Où devrait vivre la logique de désallocation ?

!!! example "Exercice 2 -- Handler trop gros"
    Un collègue écrit un handler de 30 lignes qui vérifie le stock disponible, applique des promotions, calcule les frais de port et envoie un email. Quels principes sont violés ? Comment le refactorer ?

!!! example "Exercice 3 -- Ajouter un endpoint CLI"
    Écrivez un point d'entrée CLI (avec `argparse` ou `click`) qui appelle `bus.handle(commands.Allouer(...))`. Vérifiez que vous n'avez rien changé dans les handlers ni le domaine.

---

## Résumé

La Service Layer est le ciment entre le monde extérieur et le modèle de domaine. Elle applique le **principe de responsabilité unique** à l'échelle des couches :

| Couche | Responsabilité | Exemple |
|--------|---------------|---------|
| **Entrypoints** | Traduire un protocole externe en commands | Flask parse le JSON, construit `commands.Allouer`, délègue au bus |
| **Service Layer** | Orchestrer le workflow applicatif | Le handler ouvre le UoW, récupère le produit, appelle `produit.allouer()`, committe |
| **Domaine** | Implémenter les règles métier | `Produit.allouer()` trie les lots, vérifie la disponibilité, choisit le lot |

Quelques principes à retenir :

- **Les handlers sont fins.** Quelques lignes de code procédural. Si un handler dépasse 15 lignes, de la logique métier s'est probablement glissée au mauvais endroit.
- **Le domaine ne sait rien de la persistance.** Il ne connaît ni le Repository, ni le Unit of Work. C'est le handler qui fait le lien.
- **Les entrypoints ne savent rien du domaine.** Flask ne manipule jamais directement un `Produit` ou un `Lot`. Il envoie des commands et reçoit des résultats.
- **Les fakes sont préférés aux mocks.** En implémentant les interfaces abstraites (`AbstractRepository`, `AbstractUnitOfWork`), on obtient des doubles de test fiables et maintenables.

!!! quote "Règle d'or"

    Si vous ne savez pas où placer un bout de code, posez-vous la question : "Est-ce une **règle métier** (domaine), une **étape du workflow** (service layer), ou une **traduction de protocole** (entrypoint) ?"

---

*Prochain chapitre : [TDD à haute et basse vitesse](chapitre_05_tdd.md) -- comment exploiter cette architecture en couches pour écrire des tests à la fois rapides et fiables.*
