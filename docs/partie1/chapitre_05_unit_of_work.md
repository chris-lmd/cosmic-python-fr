# Chapitre 5 -- Le pattern Unit of Work

> **Comment garantir que les opérations en base de données sont atomiques, sans coupler nos handlers à SQLAlchemy ?**

Une opération **atomique** est une opération indivisible : soit toutes les étapes réussissent ensemble, soit aucune n'est appliquée. Il n'y a jamais d'état intermédiaire visible.

Jusqu'ici, notre architecture repose sur un repository qui abstrait l'accès à la base de données, et une service layer qui orchestre les cas d'usage. Mais une question reste ouverte : **qui gère la transaction ?**

Dans ce chapitre, nous introduisons le pattern **Unit of Work** -- un context manager qui encapsule la session, le repository et la transaction dans un seul objet cohérent.

---

## Le problème : qui contrôle la transaction ?

Notre handler `allouer` doit faire plusieurs choses dans une seule transaction :

1. Lire un `Produit` depuis la base
2. Appeler la logique d'allocation sur le domaine
3. Persister le résultat en base
4. Commiter la transaction

La question est : **où vit la gestion de la session et du commit ?**

### Option 1 : le repository gère la session

Si le repository crée et commite lui-même sa session, chaque opération (`add`, `get`) est indépendante. On perd **l'atomicité** : si l'allocation réussit mais que le commit échoue, on se retrouve dans un état incohérent.

```python
# Problème : chaque appel est une transaction séparée
produit = repo.get("CHAISE-COMFY")          # transaction 1
réf_lot = produit.allouer(ligne)            # en mémoire
repo.save(produit)                          # transaction 2 -- et si ça échoue ?
```

### Option 2 : le handler gère la session

Si le handler crée la session SQLAlchemy et la passe au repository, on retrouve l'atomicité. Mais le handler devient **couplé à SQLAlchemy** -- exactement ce qu'on voulait éviter avec le repository.

```python
# Problème : le handler connaît SQLAlchemy
def allouer(cmd, session_factory):
    session = session_factory()
    repo = SqlAlchemyRepository(session)
    produit = repo.get(cmd.sku)
    réf_lot = produit.allouer(ligne)
    session.commit()  # le handler manipule directement la session
```

### La solution : un nouvel objet qui encapsule la transaction

Le **Unit of Work** résout ce dilemme. C'est un objet qui :

- **Crée la session** à l'entrée du context manager
- **Fournit le repository** configuré avec cette session
- **Expose `commit()` et `rollback()`** sans révéler l'implémentation
- **Ferme la session** à la sortie, avec rollback automatique si `commit()` n'a pas été appelé

Le handler n'a plus besoin de connaître SQLAlchemy. Il travaille avec une **abstraction**.

---

## Le pattern Unit of Work

Le Unit of Work représente une **unité de travail atomique**. C'est un concept formalisé par Martin Fowler dans *Patterns of Enterprise Application Architecture* : un objet qui suit les modifications faites pendant une transaction et coordonne leur écriture en base.

Dans notre implémentation, le Unit of Work est un **context manager** Python. Voici comment un handler l'utilise :

```python
def allouer(id_commande: str, sku: str, quantité: int, uow: AbstractUnitOfWork) -> str:
    ligne = LigneDeCommande(id_commande=id_commande, sku=sku, quantité=quantité)
    with uow:
        produit = uow.produits.get(sku=sku)
        if produit is None:
            raise SkuInconnu(f"SKU inconnu : {sku}")
        réf_lot = produit.allouer(ligne)
        uow.commit()
    return réf_lot
```

Comparé au chapitre 4, où les handlers recevaient `repo` et `session` séparément, le UoW regroupe ces deux dépendances en un seul objet cohérent. Le handler n'a plus besoin de savoir comment le repository et la session sont construits.

Les règles sont simples :

- `with uow:` ouvre la transaction et initialise le repository
- `uow.produits` donne accès au repository (sans savoir comment il est construit)
- `uow.commit()` valide la transaction et marque le flag `_committed`
- Si une exception survient avant le commit, `__exit__` déclenche un **rollback automatique** (uniquement si `_committed` est `False`)
- La session est fermée dans tous les cas

---

## L'interface abstraite : `AbstractUnitOfWork`

L'interface est définie comme une classe abstraite qui implémente le **context manager protocol** de Python -- c'est-à-dire les méthodes `__enter__` et `__exit__`.

```python title="src/allocation/service_layer/unit_of_work.py" hl_lines="5 8 11 14 19"
class AbstractUnitOfWork(abc.ABC):
    """
    Interface abstraite du Unit of Work.

    Définit le contrat : un repository produits,
    et les méthodes commit/rollback.
    """

    produits: repository.AbstractRepository

    def __enter__(self) -> AbstractUnitOfWork:
        self._committed = False
        return self

    def __exit__(self, *args: object) -> None:
        if not self._committed:
            self.rollback()

    def commit(self) -> None:
        self._commit()
        self._committed = True

    @abc.abstractmethod
    def _commit(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def rollback(self) -> None:
        raise NotImplementedError
```

### Le flag `_committed` : un rollback conditionnel

Notre implémentation utilise un **flag `_committed`** pour ne faire le rollback que si nécessaire, plutôt que d'appeler `self.rollback()` de manière inconditionnelle dans `__exit__`. Voici l'intérêt de cette approche :

1. **`__enter__`** initialise `self._committed = False` à chaque entrée dans le context manager. Cela garantit un état propre pour chaque transaction.

2. **`commit()`** appelle d'abord `_commit()` (l'implémentation concrète), puis positionne `self._committed = True`. L'ordre est important : si `_commit()` lève une exception, le flag reste `False` et le rollback sera effectué.

3. **`__exit__`** vérifie `self._committed` avant d'appeler `rollback()`. Après un commit réussi, le rollback est inutile et pourrait même être problématique (certains drivers de base de données n'apprécient pas un rollback après un commit).

```python
# Scénario 1 : commit réussi
with uow:                    # _committed = False
    # ... opérations ...
    uow.commit()             # _commit() OK → _committed = True
# __exit__ : _committed est True → pas de rollback

# Scénario 2 : exception avant le commit
with uow:                    # _committed = False
    # ... opérations ...
    raise ValueError("oops") # exception !
# __exit__ : _committed est False → rollback()

# Scénario 3 : erreur pendant le commit
with uow:                    # _committed = False
    # ... opérations ...
    uow.commit()             # _commit() lève une exception → _committed reste False
# __exit__ : _committed est False → rollback()
```

### Anatomie du context manager protocol

Le protocol `with` de Python repose sur deux méthodes spéciales :

| Méthode       | Quand ?                                 | Rôle dans le UoW                          |
|---------------|----------------------------------------|-------------------------------------------|
| `__enter__`   | À l'entrée du bloc `with`              | Initialise `_committed = False`, retourne `self` |
| `__exit__`    | À la sortie du bloc `with` (toujours)  | Rollback conditionnel si pas de commit    |

Le point crucial est que `__exit__` est **toujours appelé**, même si une exception a lieu. Grâce au flag `_committed`, le rollback n'est déclenché que quand il est réellement nécessaire.

!!! note "Pourquoi `_commit` avec un underscore ?"
    La méthode publique `commit()` est définie dans la classe abstraite. Elle délègue à `_commit()`, la méthode abstraite que les sous-classes implémentent, puis positionne le flag `_committed`. Ce découpage permet d'ajouter de la logique commune dans `commit()` (le tracking du flag) sans que chaque implémentation doive y penser.

---

## L'implémentation SQLAlchemy

Voici l'implémentation concrète qui utilise SQLAlchemy :

```python title="src/allocation/service_layer/unit_of_work.py"
DEFAULT_SESSION_FACTORY = sessionmaker(
    bind=create_engine(
        "sqlite:///allocation.db",
        isolation_level="SERIALIZABLE",
    )
)


class SqlAlchemyUnitOfWork(AbstractUnitOfWork):
    """
    Implémentation concrète du UoW avec SQLAlchemy.

    Gère la session SQLAlchemy et le repository associé.
    """

    def __init__(self, session_factory: sessionmaker = DEFAULT_SESSION_FACTORY):
        self.session_factory = session_factory

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        self.session: Session = self.session_factory()
        self.produits = repository.SqlAlchemyRepository(self.session)
        return super().__enter__()

    def __exit__(self, *args: object) -> None:
        super().__exit__(*args)
        self.session.close()

    def _commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()
```

### Le cycle de vie de la session

Voici ce qui se passe concrètement lors de l'exécution d'un handler :

```
with uow:                          # (1) __enter__ est appelé
    |                              #     -> session = session_factory()
    |                              #     -> produits = SqlAlchemyRepository(session)
    |                              #     -> _committed = False
    produit = uow.produits.get()   # (2) lecture via la session
    produit.allouer(ligne)         # (3) logique métier pure
    uow.commit()                   # (4) session.commit() + _committed = True
                                   # (5) __exit__ est appelé
                                   #     -> _committed est True → pas de rollback
                                   #     -> session.close()
```

Trois points importants :

1. **La session est créée à l'entrée** (`__enter__`), pas dans le constructeur. Cela signifie qu'on peut réutiliser un UoW pour plusieurs transactions successives.

2. **Le rollback dans `__exit__` est conditionnel.** Grâce au flag `_committed`, le rollback n'est appelé que si `commit()` n'a pas été exécuté avec succès. C'est plus propre qu'un rollback inconditionnel qui serait exécuté même après un commit réussi.

3. **La session est toujours fermée** à la sortie, que la transaction ait réussi ou non. Pas de fuite de connexion.

!!! warning "Isolation level et optimistic locking"
    La session factory utilise le niveau d'isolation `SERIALIZABLE` pour garantir la cohérence des lectures au sein d'une transaction. Mais ce n'est **pas** ce qui empêche deux transactions de modifier le même `Produit` simultanément.

    La protection contre les **race conditions** vient du **numéro_version** (optimistic locking), introduit au [chapitre 2](../partie1/chapitre_02_aggregats.md) : au moment du `UPDATE`, SQLAlchemy ajoute une clause `WHERE numéro_version = N`. Si une autre transaction a déjà incrémenté le numéro, la clause ne matche aucune ligne et l'opération échoue. C'est ce mécanisme -- pas le niveau d'isolation -- qui empêche la surallocation.

---

!!! info "Et les Domain Events ?"
    Le Unit of Work joue un rôle supplémentaire que nous découvrirons au [chapitre 7](../partie2/chapitre_07_events.md) : la **collecte des events** émis par les agrégats pendant la transaction. Pour l'instant, concentrons-nous sur son rôle de gestionnaire de transactions.

---

## Le Fake Unit of Work pour les tests

L'un des avantages majeurs du pattern est la **testabilité**. Puisque les handlers dépendent de `AbstractUnitOfWork` (une abstraction), on peut facilement le remplacer par un fake dans les tests unitaires. Le `FakeUnitOfWork` utilise un `FakeRepository` qui stocke les produits en mémoire (un simple `set`) :

```python title="tests/unit/test_handlers.py"
class FakeUnitOfWork(unit_of_work.AbstractUnitOfWork):
    """
    Unit of Work en mémoire pour les tests.

    Le flag `_committed` est géré par la classe parente (AbstractUnitOfWork).
    """

    def __init__(self) -> None:
        self.produits = FakeRepository()

    def __enter__(self) -> FakeUnitOfWork:
        return super().__enter__()

    def _commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass
```

### Le flag `_committed` géré par la classe parente

Le tracking du commit est délégué à la classe parente `AbstractUnitOfWork` via le flag `_committed`. C'est la méthode `commit()` de la classe abstraite qui :

1. Appelle `_commit()` (ici un no-op dans le fake)
2. Positionne `self._committed = True`

Le `FakeUnitOfWork` n'a pas besoin de gérer ce flag lui-même. Les tests peuvent vérifier que le commit a bien eu lieu en accédant à `uow._committed` :

```python
class TestAjouterLot:
    def test_ajouter_un_lot(self):
        uow = FakeUnitOfWork()
        handlers.ajouter_lot("b1", "COUSSIN-CARRE", 100, None, uow)

        assert uow.produits.get("COUSSIN-CARRE") is not None
        assert uow._committed  # on vérifie que le commit a eu lieu
```

Cette approche teste le même chemin de code que la production (`commit()` -> `_commit()` -> `_committed = True`). Si la logique de `commit()` change dans la classe abstraite, les tests en bénéficient automatiquement.

### Le `FakeRepository` et l'attribut `seen`

Le `FakeRepository` hérite de `AbstractRepository`, qui définit l'attribut `seen`. Les tests unitaires vérifient donc le comportement complet du UoW, y compris le rollback conditionnel grâce au flag `_committed`.

!!! tip "Le pattern général des fakes"
    Un bon fake implémente la même interface que le composant réel, avec un stockage en mémoire. La logique partagée (comme le flag `_committed`) vit dans la classe abstraite, ce qui garantit un comportement identique entre le fake et l'implémentation réelle. C'est plus fiable qu'un mock car on teste le **comportement** réel de l'interface, pas juste les appels de méthodes.

---

## Le flux complet : du handler au domaine

Voici le flux complet quand un handler traite une demande d'allocation :

```
handler: allouer(id_commande, sku, quantité, uow)
    |
    +---> with uow:                          # UoW.__enter__
    |         |                               #   crée session + repository
    |         |                               #   _committed = False
    |         +---> uow.produits.get(sku)     # Repository.get()
    |         |                               #   marque le Produit comme "seen"
    |         +---> produit.allouer(ligne)    # Logique métier pure
    |         |
    |         +---> uow.commit()              # UoW.commit()
    |                                         #   _commit() + _committed = True
    +---> (sortie du with)                    # UoW.__exit__
                                              #   _committed=True → pas de rollback
                                              #   session.close()
```

---

## Résumé

Le pattern **Unit of Work** résout le problème de la gestion des transactions en introduisant un objet qui encapsule la session, le repository et la logique de commit/rollback.

### Ce que le Unit of Work apporte

| Aspect                | Sans UoW                              | Avec UoW                                |
|-----------------------|---------------------------------------|------------------------------------------|
| Transaction           | Gérée par le handler ou le repository | Encapsulée dans le context manager       |
| Atomicité             | Difficile à garantir                  | Garantie par `__enter__`/`__exit__`      |
| Couplage              | Handler couplé à SQLAlchemy           | Handler dépend d'une abstraction         |
| Testabilité           | Nécessite une base de données         | Fake UoW en mémoire                      |
| Rollback              | Inconditionnel ou oublié              | Conditionnel via le flag `_committed`    |

### Les fichiers clés

| Fichier | Rôle |
|---------|------|
| `src/allocation/service_layer/unit_of_work.py` | Interface abstraite et implémentation SQLAlchemy |
| `src/allocation/adapters/repository.py` | Repository avec tracking des agrégats vus (`seen`) |
| `src/allocation/service_layer/handlers.py` | Handlers qui utilisent le UoW comme context manager |
| `tests/unit/test_handlers.py` | `FakeUnitOfWork` et `FakeRepository` pour les tests |

### Les principes à retenir

1. **Le UoW est un context manager** qui gère le cycle de vie de la transaction : ouverture, commit, rollback, fermeture.
2. **Le handler ne connaît que l'abstraction** (`AbstractUnitOfWork`), jamais SQLAlchemy directement.
3. **Le rollback est conditionnel** : grâce au flag `_committed`, `__exit__` ne fait le rollback que si `commit()` n'a pas été appelé avec succès.
4. **Le `FakeUnitOfWork` hérite du comportement `_committed`** de la classe parente, garantissant un comportement identique entre les tests et la production.

## Exercices

!!! example "Exercice 1 -- UoW avec rollback explicite"
    Modifiez le `FakeUnitOfWork` pour que `rollback()` vide le `FakeRepository`. Écrivez un test qui vérifie qu'après un rollback, les produits ajoutés pendant la transaction ont disparu.

!!! example "Exercice 2 -- Double commit"
    Que se passe-t-il si un handler appelle `uow.commit()` deux fois ? Et si `commit()` n'est jamais appelé ? Écrivez des tests pour vérifier chaque scénario.

!!! example "Exercice 3 -- UoW sans context manager"
    Essayez d'utiliser le `SqlAlchemyUnitOfWork` sans `with` (en appelant manuellement `__enter__` et `__exit__`). Quels risques cela crée-t-il ? Pourquoi le context manager est-il préférable ?

---

!!! abstract "Dans le prochain chapitre"
    Nous verrons comment et pourquoi introduire des **abstractions** pour découpler les couches de notre architecture.

*Prochain chapitre : [Couplage et abstractions](chapitre_06_abstractions.md) -- pourquoi et comment introduire des abstractions pour découpler les couches.*
