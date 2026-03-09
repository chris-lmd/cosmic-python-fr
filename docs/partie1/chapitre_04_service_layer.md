# Chapitre 4 -- La Service Layer

## Le problème : des routes Flask qui grossissent

Dans les chapitres précédents, nous avons construit un modèle de domaine (`Lot`, `LigneDeCommande`, `allouer()`) et un Repository pour persister nos objets dans un conteneur `Produit`. La logique d'allocation que nous avions écrite comme fonction libre au chapitre 1 est maintenant une **méthode** de `Produit` -- c'est lui qui possède les lots et qui sait comment les trier. Imaginons maintenant une première route FastAPI pour allouer du stock :

```python
# Version naïve -- toute la logique dans la route
@app.post("/allocate")
def allocate_endpoint(data: dict):
    # 1. Ouvrir une session / transaction
    session = get_session()
    # 2. Récupérer l'agrégat
    produit = repo.get(data["sku"])
    if produit is None:
        raise HTTPException(status_code=400, detail="SKU inconnu")
    # 3. Construire le value object
    ligne = LigneDeCommande(data["id_commande"], data["sku"], data["quantité"])
    # 4. Appeler la logique métier
    réf_lot = produit.allouer(ligne)
    # 5. Committer
    session.commit()
    return {"réf_lot": réf_lot}
```

Ce code fonctionne, mais il pose plusieurs problèmes :

**Duplication.** Si demain on ajoute une CLI, un worker Celery ou un consumer Redis, il faudra recopier toute cette séquence (récupérer le produit, construire la ligne, allouer, committer). Chaque point d'entrée réimplémentera le même workflow.

**Testabilité.** Pour tester cette logique, on doit démarrer le serveur, envoyer de vraies requêtes HTTP et souvent brancher une base de données. Les tests deviennent lents et fragiles.

**Responsabilité mal placée.** FastAPI est un framework web. Son rôle est de convertir des requêtes HTTP en appels applicatifs, pas d'orchestrer un workflow métier.

---

## La Service Layer : une couche d'orchestration

La Service Layer est une couche mince qui se place **entre** les points d'entrée (FastAPI, CLI, Consumer...) et le modèle de domaine. Son rôle est précis :

1. Récupérer les objets nécessaires via le Repository
2. Appeler les méthodes du domaine
3. Committer la transaction

Elle **ne contient pas** de logique métier. La logique métier reste dans le modèle de domaine (c'est `Produit.allouer()` qui décide quel lot choisir, pas le handler). La Service Layer se contente de **coordonner**.

```
┌──────────────────────────────────────────────┐
│              Entrypoints                      │
│         (FastAPI, CLI, Consumer...)           │
│  Convertit le protocole externe en appels     │
└──────────────────┬───────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────┐
│            Service Layer                      │
│         (handlers.py)                         │
│  Orchestre : Repository + Session → Domaine   │
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

Nos handlers vivent dans `src/allocation/service_layer/handlers.py`. Chaque handler prend les paramètres métier ainsi que le **repository** et la **session**, puis orchestre le workflow en quelques lignes.

### `ajouter_lot` -- créer un lot de stock

```python
def ajouter_lot(
    réf: str, sku: str, quantité: int, eta: date | None,
    repo: AbstractRepository, session,
) -> None:
    produit = repo.get(sku=sku)
    if produit is None:
        produit = model.Produit(sku=sku, lots=[])
        repo.add(produit)
    produit.lots.append(model.Lot(réf=réf, sku=sku, quantité=quantité, eta=eta))
    session.commit()
```

Le handler est **procédural** : il récupère ou crée le produit via le repository, ajoute le lot, puis committe la session. Pas de boucle complexe, pas de logique conditionnelle métier.

### `allouer` -- allouer une ligne de commande

```python
def allouer(
    id_commande: str, sku: str, quantité: int,
    repo: AbstractRepository, session,
) -> str:
    ligne = model.LigneDeCommande(
        id_commande=id_commande, sku=sku, quantité=quantité
    )
    produit = repo.get(sku=sku)
    if produit is None:
        raise SkuInconnu(f"SKU inconnu : {sku}")
    réf_lot = produit.allouer(ligne)
    session.commit()
    return réf_lot
```

Observez que **toute la logique d'allocation** (trier les lots par ETA, vérifier la quantité disponible, choisir le meilleur lot) est dans `produit.allouer()`. Le handler ne fait que préparer les données et déclencher l'appel.

!!! info "Vers le Unit of Work"

    Vous remarquerez que nos handlers reçoivent `repo` et `session` séparément. Au [chapitre 5](chapitre_05_unit_of_work.md), nous verrons comment le pattern **Unit of Work** encapsule ces deux dépendances dans un seul objet cohérent.

### La ligne de démarcation

Un bon test pour savoir si la logique est au bon endroit : si vous enlevez le handler et appelez directement `produit.allouer()` dans un test unitaire, la règle métier fonctionne-t-elle toujours ? Si oui, la logique est bien dans le domaine. Le handler ne fait que du "plumbing".

---

## FastAPI comme thin adapter

Maintenant que la Service Layer existe, FastAPI n'a plus qu'un seul rôle : **traduire le protocole HTTP** en appels que la couche service comprend, puis convertir le résultat en réponse HTTP.

Voici le code réel de `src/allocation/entrypoints/fastapi_app.py` :

```python
# src/allocation/entrypoints/fastapi_app.py
from fastapi import FastAPI, HTTPException

app = FastAPI()

@app.post("/add_batch", status_code=201)
def add_batch_endpoint(data: dict):
    eta = data.get("eta")
    if eta is not None:
        eta = datetime.fromisoformat(eta).date()
    handlers.ajouter_lot(
        réf=data["ref"], sku=data["sku"],
        quantité=data["qty"], eta=eta,
        repo=repo, session=session,
    )
    return "OK"

@app.post("/allocate", status_code=201)
def allocate_endpoint(data: dict):
    try:
        réf_lot = handlers.allouer(
            id_commande=data["orderid"], sku=data["sku"],
            quantité=data["qty"],
            repo=repo, session=session,
        )
    except handlers.SkuInconnu as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"batchref": réf_lot}
```

Chaque endpoint suit la même structure en trois temps :

1. **Extraire** les données de la requête HTTP
2. **Appeler** le handler avec les paramètres métier et les dépendances d'infrastructure
3. **Retourner** le résultat sous forme de réponse HTTP

Il n'y a **aucune logique métier** dans ces fonctions. Pas de `if` sur la disponibilité du stock, pas de tri des lots, pas d'accès direct au Repository. FastAPI ne sait même pas que des lots existent.

!!! tip "Adaptateurs et ports"

    FastAPI est un **adapter** au sens de l'architecture hexagonale. Il adapte le port HTTP vers l'interface de la Service Layer. Si demain vous remplacez FastAPI par un autre framework, seul cet adaptateur change -- ni les handlers, ni le domaine ne sont touchés.

---

## Testabilité : des fakes plutôt que des mocks

L'un des gains majeurs de la Service Layer est la **testabilité**. On peut tester toute l'orchestration sans démarrer FastAPI et sans toucher à la base de données, en remplaçant les adaptateurs concrets par des fakes.

### FakeRepository et FakeSession

On utilise le `FakeRepository` défini au [chapitre 3](chapitre_03_repository.md) et une `FakeSession` toute simple qui trace les commits. Ces fakes sont des implémentations **en mémoire** des abstractions : le `FakeRepository` stocke les produits dans un `set` Python, et la `FakeSession` enregistre si `commit()` a été appelé, sans toucher à aucune base de données.

```python
class FakeSession:
    committed = False
    def commit(self):
        self.committed = True
```

### Les tests des handlers

Avec ces fakes, tester un handler est direct et rapide :

```python
class TestAjouterLot:
    def test_ajouter_lot_pour_nouveau_produit(self):
        repo = FakeRepository()
        session = FakeSession()
        handlers.ajouter_lot("l1", "COUSSIN-CARRE", 100, None, repo, session)

        assert repo.get("COUSSIN-CARRE") is not None
        assert session.committed

class TestAllouer:
    def test_allouer_retourne_ref_lot(self):
        repo = FakeRepository()
        session = FakeSession()
        handlers.ajouter_lot("l1", "CHAISE-COMFY", 100, None, repo, session)
        réf_lot = handlers.allouer("c1", "CHAISE-COMFY", 10, repo, session)

        assert réf_lot == "l1"

    def test_allouer_erreur_pour_sku_inconnu(self):
        repo = FakeRepository()
        session = FakeSession()
        with pytest.raises(handlers.SkuInconnu, match="SKU-INEXISTANT"):
            handlers.allouer("c1", "SKU-INEXISTANT", 10, repo, session)
```

Remarquez ce que ces tests **ne font pas** :

- Pas de `app.test_client()` -- aucune requête HTTP
- Pas de `session` SQLAlchemy -- aucune base de données
- Pas de `mock.patch` -- on injecte de vrais objets (les fakes)

Les tests sont rapides (millisecondes), isolés et lisibles. Ils vérifient le **comportement métier** (est-ce que le lot est bien créé ? est-ce que l'allocation retourne la bonne référence ?) sans être couplés à aucune infrastructure.

### Et les tests de l'API ?

Les tests de l'API FastAPI deviennent des **tests d'intégration légers** : ils vérifient uniquement que FastAPI parse correctement le JSON, appelle le bon handler, et retourne le bon code HTTP. La logique métier, elle, est déjà couverte par les tests unitaires des handlers.

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

!!! example "Exercice 1 -- Nouveau handler"
    Ajoutez un handler `désallouer` qui prend les paramètres `id_commande, sku, quantité, repo, session` et retire une allocation. Écrivez le test correspondant avec un `FakeRepository` et une `FakeSession`. Où devrait vivre la logique de désallocation ?

!!! example "Exercice 2 -- Handler trop gros"
    Un collègue écrit un handler de 30 lignes qui vérifie le stock disponible, applique des promotions, calcule les frais de port et envoie un email. Quels principes sont violés ? Comment le refactorer ?

!!! example "Exercice 3 -- Ajouter un endpoint CLI"
    Écrivez un point d'entrée CLI (avec `argparse` ou `click`) qui appelle `handlers.allouer(...)`. Vérifiez que vous n'avez rien changé dans les handlers ni le domaine.

---

## Résumé

La Service Layer est le ciment entre le monde extérieur et le modèle de domaine. Elle applique le **principe de responsabilité unique** à l'échelle des couches :

| Couche | Responsabilité | Exemple |
|--------|---------------|---------|
| **Entrypoints** | Traduire un protocole externe en appels au handler | FastAPI parse le JSON, appelle le handler avec les bons paramètres |
| **Service Layer** | Orchestrer le workflow applicatif | Le handler récupère le produit via le repo, appelle `produit.allouer()`, committe la session |
| **Domaine** | Implémenter les règles métier | `Produit.allouer()` trie les lots, vérifie la disponibilité, choisit le lot |

Quelques principes à retenir :

- **Les handlers sont fins.** Quelques lignes de code procédural. Si un handler dépasse 15 lignes, de la logique métier s'est probablement glissée au mauvais endroit.
- **Le domaine ne sait rien de la persistance.** Il ne connaît ni le Repository, ni la session. C'est le handler qui fait le lien.
- **Les entrypoints ne savent rien du domaine.** FastAPI ne manipule jamais directement un `Produit` ou un `Lot`. Il appelle les handlers et retourne les résultats.
- **Les fakes sont préférés aux mocks.** En implémentant les interfaces abstraites (`AbstractRepository`), on obtient des doubles de test fiables et maintenables.

!!! quote "Règle d'or"

    Si vous ne savez pas où placer un bout de code, posez-vous la question : "Est-ce une **règle métier** (domaine), une **étape du workflow** (service layer), ou une **traduction de protocole** (entrypoint) ?"

---

*Prochain chapitre : [Le pattern Unit of Work](chapitre_05_unit_of_work.md) -- comment encapsuler la session et le repository dans un context manager pour garantir l'atomicité.*