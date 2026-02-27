# Patterns d'Architecture en Python

> Guide francophone des patterns d'architecture logicielle en Python

## De quoi parle ce guide ?

Ce guide vous accompagne dans la construction d'une **application Python bien architecturée**, en introduisant progressivement des patterns éprouvés issus du **Domain-Driven Design (DDD)** et de l'**architecture événementielle**.

Le fil rouge est un système d'**allocation de stock** : un problème métier suffisamment réaliste pour illustrer les enjeux, mais suffisamment simple pour se concentrer sur l'architecture.

## Positionnement de ce guide

Ce guide adopte une approche combinant **Domain-Driven Design (DDD)**, **architecture hexagonale (Ports & Adapters)** et **architecture événementielle**. Si vous avez déjà entendu parler de la **Clean Architecture** de Robert C. Martin, vous retrouverez ici les mêmes principes fondamentaux — isolation du domaine, inversion des dépendances, découplage de l'infrastructure — mais avec des patterns et un vocabulaire mieux adaptés à Python.

Là où la Clean Architecture propose des Use Cases sous forme de classes et un découpage en 4 couches formelles (pensé pour Java/C#), ce guide privilégie des **handlers légers** (de simples fonctions), des patterns éprouvés comme le **Repository** et le **Unit of Work**, et une architecture événementielle complète (Events, Commands, Message Bus, CQRS) qui permet au système de grandir progressivement.

| Aspect | Clean Architecture (Uncle Bob) | Ce guide (DDD + Hexagonal) |
|--------|------|------|
| **Orchestration** | Use Case (une classe avec `execute()`) | Handler (une simple fonction) |
| **Formatage de la sortie** | Presenter dédié | Retour direct, l'entrypoint formate |
| **Couches** | 4 couches strictes et formelles | Ajoutées progressivement selon le besoin |
| **Architecture événementielle** | Peu détaillée | Centrale (Events, Commands, Message Bus, CQRS) |
| **Modélisation du domaine** | Peu détaillée | Riche (Aggregates, Value Objects, Domain Events) |
| **Persistance** | Gateways (concept générique) | Repository + Unit of Work (patterns éprouvés) |

En résumé : même philosophie, outillage différent, taillé pour Python.

## Pourquoi ces patterns ?

La plupart des applications commencent simples, puis deviennent difficiles à maintenir au fil du temps. La logique métier se disperse entre les routes de l'API, les requêtes SQL, et les scripts utilitaires. Les tests deviennent fragiles car couplés à la base de données.

Les patterns présentés ici résolvent ces problèmes en :

- **Isolant la logique métier** dans un modèle de domaine pur (sans dépendance à la BDD ou au framework web)
- **Découplant les couches** grâce à des abstractions (Repository, Unit of Work)
- **Rendant le système extensible** grâce à une architecture événementielle (Events, Message Bus, CQRS)

## Organisation du guide

### Partie 1 — Construire une architecture pour le Domain Modeling

| Chapitre | Pattern | Problème résolu |
|----------|---------|-----------------|
| [1. Le modèle de domaine](partie1/chapitre_01_modele_domaine.md) | Domain Model | Où mettre la logique métier ? |
| [2. Le pattern Repository](partie1/chapitre_02_repository.md) | Repository | Comment découpler le domaine de la BDD ? |
| [3. Couplage et abstractions](partie1/chapitre_03_abstractions.md) | Dependency Inversion | Pourquoi et comment introduire des abstractions ? |
| [4. La Service Layer](partie1/chapitre_04_service_layer.md) | Service Layer | Où placer l'orchestration ? |
| [5. TDD à haute et basse vitesse](partie1/chapitre_05_tdd.md) | Testing Pyramid | Comment tester efficacement ? |
| [6. Le pattern Unit of Work](partie1/chapitre_06_unit_of_work.md) | Unit of Work | Comment gérer les transactions ? |
| [7. Agrégats et frontières](partie1/chapitre_07_aggregats.md) | Aggregate | Comment garantir la cohérence ? |

### Partie 2 — Architecture événementielle

| Chapitre | Pattern | Problème résolu |
|----------|---------|-----------------|
| [8. Events et le Message Bus](partie2/chapitre_08_events.md) | Domain Events | Comment réagir aux changements du domaine ? |
| [9. Aller plus loin avec le Message Bus](partie2/chapitre_09_message_bus.md) | Message Bus | Comment le bus devient le coeur de l'architecture ? |
| [10. Commands](partie2/chapitre_10_commands.md) | Command | Comment distinguer intentions et faits ? |
| [11. Events externes](partie2/chapitre_11_events_externes.md) | Integration Events | Comment communiquer entre services ? |
| [12. CQRS](partie2/chapitre_12_cqrs.md) | CQRS | Comment optimiser lectures et écritures séparément ? |
| [13. Injection de dépendances](partie2/chapitre_13_injection_dependances.md) | Dependency Injection | Comment assembler les composants proprement ? |

## Le domaine métier : l'allocation de stock

Imaginons une entreprise de e-commerce qui doit gérer son stock. Quand un client passe commande, le système doit **allouer** les produits commandés à des **lots de stock** (batches).

Les règles métier :

- Chaque lot a une **référence**, un **SKU** (identifiant produit), une **quantité**, et une **ETA** (date d'arrivée) optionnelle
- Les lots en stock (sans ETA) sont **préférés** aux lots en transit
- Parmi les lots en transit, on choisit celui avec l'**ETA la plus proche**
- On ne peut pas allouer plus que la quantité disponible

```python
@dataclass(frozen=True)
class OrderLine:
    """Value Object : une ligne de commande."""
    orderid: str
    sku: str
    qty: int

class Batch:
    """Entity : un lot de stock."""
    def __init__(self, ref, sku, qty, eta=None):
        self.reference = ref
        self.sku = sku
        self.eta = eta
        self._purchased_quantity = qty
        self._allocations: set[OrderLine] = set()
```

## Pré-requis

- Python 3.12+
- Notions de base en programmation orientée objet
- Familiarité avec pytest
- Curiosité pour l'architecture logicielle

## Crédits

Ce guide s'inspire des concepts présentés dans *Architecture Patterns with Python* de Harry Percival et Bob Gregory (disponible sur [cosmicpython.com](https://www.cosmicpython.com/)), ainsi que des travaux fondateurs d'Eric Evans (Domain-Driven Design) et de Martin Fowler (Patterns of Enterprise Application Architecture).

Ce guide a été rédigé avec l'assistance de l'IA générative Claude (Anthropic). Le contenu, les explications et les exemples de code sont originaux mais ont été générés et structurés à l'aide de cet outil.

## Licence

Ce contenu est mis à disposition sous licence [Creative Commons Attribution-NonCommercial-ShareAlike 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
