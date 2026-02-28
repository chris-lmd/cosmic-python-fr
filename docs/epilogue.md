# Épilogue -- Pièges, compromis et conseils pragmatiques

Vous avez parcouru 13 chapitres. Vous connaissez maintenant le Domain Model, le Repository, la Service Layer, le Unit of Work, les Agrégats, les Events, le Message Bus, les Commands, le CQRS et l'Injection de dépendances. C'est un arsenal puissant.

Mais ces patterns ne sont pas gratuits. Ils ajoutent de la complexité, de l'indirection et des abstractions. Ce chapitre de clôture est là pour prendre du recul et poser les questions que l'enthousiasme du début fait parfois oublier.

---

## L'architecture complète en un coup d'œil

```
┌──────────────────────────────────────────────────────────────────────┐
│                          BOOTSTRAP                                   │
│               (Composition Root — bootstrap.py)                      │
│                                                                      │
│  Crée :  UoW, Notifications, Publisher                               │
│  Configure : routing commands/events → handlers                      │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
        ┌──────────────────▼──────────────────────┐
        │              ENTRYPOINTS                 │
        │   Flask API  │  Redis Consumer  │  CLI   │
        │   (thin adapters — traduisent le         │
        │    protocole externe en commands)         │
        └──────────────────┬──────────────────────┘
                           │ Command
                           ▼
        ┌──────────────────────────────────────────┐
        │              MESSAGE BUS                  │
        │                                          │
        │  queue = [command]                       │
        │  while queue:                            │
        │      dispatch → handler                  │
        │      collect_new_events → queue           │
        └──────┬─────────────────────┬─────────────┘
               │                     │
        Command Handlers       Event Handlers
               │                     │
               ▼                     ▼
        ┌──────────────┐     ┌──────────────────┐
        │  UNIT OF WORK │     │  Notifications   │
        │  (transaction)│     │  Publisher        │
        │  + Repository │     │  Read Model       │
        └──────┬───────┘     └──────────────────┘
               │
               ▼
        ┌──────────────────────────────────────────┐
        │           MODÈLE DE DOMAINE               │
        │                                          │
        │  Produit (Aggregate Root)                │
        │    ├── Lot (Entity)                      │
        │    │     └── {LigneDeCommande} (VO)      │
        │    ├── numéro_version                    │
        │    └── événements[]                      │
        └──────────────────────────────────────────┘
```

Chaque flèche pointe vers l'intérieur : les dépendances vont de l'infrastructure vers le domaine, jamais l'inverse. Le domaine ne sait rien du monde extérieur.

---

## Quand NE PAS utiliser ces patterns

C'est la question la plus importante de ce guide, et elle arrive en dernier à dessein.

### L'application est un CRUD simple

Si votre application lit et écrit des lignes dans une base de données sans logique métier complexe, un framework comme Django avec ses modèles "fat" ou FastAPI avec SQLModel sera **infiniment plus productif** que cette architecture. Les patterns présentés ici n'apportent de la valeur que quand la logique métier est complexe.

### L'équipe est petite et le projet est jeune

Une équipe de 1 à 3 développeurs sur un projet de quelques mois ne tirera pas assez de bénéfices de cette architecture pour justifier sa complexité. Commencez simple, et refactorez vers ces patterns quand la douleur se fait sentir.

### Vous n'avez pas d'expert DDD dans l'équipe

Ces patterns nécessitent une compréhension partagée. Si l'équipe ne comprend pas la distinction entre Entity et Value Object, ou entre Command et Event, l'architecture deviendra un fardeau plutôt qu'un outil.

!!! tip "La règle pragmatique"
    Commencez avec l'architecture la plus simple qui fonctionne. Ajoutez des patterns **quand la complexité métier l'exige**, pas par anticipation. Le refactoring est moins coûteux qu'une abstraction inutile.

---

## Les pièges courants

### 1. Mettre de la logique métier dans les handlers

Le symptôme : un handler de 30 lignes avec des `if/else`, des boucles et des calculs. La règle : si vous enlevez le handler et appelez directement le domaine dans un test, la logique fonctionne-t-elle ? Si non, de la logique métier s'est glissée dans le handler.

### 2. Oublier de commiter

Le `with uow` garantit un rollback si `commit()` n'est pas appelé. C'est un filet de sécurité, pas un comportement normal. Si vos tests passent mais que les données ne sont pas persistées en production, vérifiez que `uow.commit()` est bien appelé explicitement.

### 3. Confondre events et commands

Les events sont des **faits** (passé composé, broadcast, erreur logguée). Les commands sont des **intentions** (impératif, point-à-point, erreur propagée). Nommer un event à l'impératif ou traiter une command comme un broadcast est un signe de confusion.

### 4. Agrégats trop gros

Si votre agrégat contient 50 entités et 200 attributs, chaque modification verrouille tout. Le bon agrégat est le plus petit groupe d'objets qui doit rester cohérent. En cas de doute, commencez petit et élargissez si nécessaire.

### 5. Tester les mocks plutôt que le comportement

Si vos tests vérifient que `repo.add()` a été appelé avec tel argument (`mock.assert_called_with`), vous testez l'implémentation, pas le comportement. Utilisez des fakes et vérifiez les résultats observables.

### 6. CQRS partout

Le CQRS est puissant mais coûteux : une table supplémentaire, des event handlers de synchronisation, de l'eventual consistency. Ne l'appliquez qu'aux requêtes de lecture qui posent réellement un problème de performance ou de complexité.

### 7. Boucles infinies dans le message bus

Si un event handler émet le même event qu'il traite, le bus tourne indéfiniment. Soyez attentif aux cascades d'events et considérez un compteur de profondeur maximale si votre système devient complexe.

---

## Les compromis à assumer

Chaque pattern de ce guide implique un compromis. Les voici résumés :

| Pattern | Ce que vous gagnez | Ce que vous payez |
|---------|-------------------|-------------------|
| **Domain Model** | Logique métier isolée, testable | Complexité initiale, mapping ORM |
| **Repository** | Découplage de la persistance | Couche d'abstraction supplémentaire |
| **Service Layer** | Orchestration claire, handlers fins | Un fichier de plus, risque de "pass-through" |
| **Unit of Work** | Transactions atomiques, events | Complexité du context manager |
| **Agrégats** | Cohérence garantie | Contention en cas d'agrégat trop gros |
| **Events** | Découplage, extensibilité | Flux indirect, plus difficile à suivre |
| **Message Bus** | Point d'entrée unique | Indirection, cascade potentielle |
| **CQRS** | Performances de lecture | Eventual consistency, table à synchroniser |
| **DI/Bootstrap** | Testabilité, flexibilité | Indirection, "magie" de l'introspection |

---

## Conseils pour démarrer un nouveau projet

1. **Commencez par le domaine.** Écrivez les classes du domaine et leurs tests unitaires avant toute infrastructure. Si le domaine fonctionne en mémoire, vous êtes sur la bonne voie.

2. **Ajoutez le Repository et le UoW ensemble.** Ils forment un couple naturel. Créez le fake en même temps que l'implémentation réelle.

3. **Le Message Bus peut attendre.** Si vous n'avez pas d'events à propager, un simple appel de fonction suffit. Ajoutez le bus quand le besoin de réactivité apparaît.

4. **CQRS est un ajout tardif.** Commencez avec le même modèle pour les lectures et les écritures. Séparez quand les requêtes de lecture deviennent un problème.

5. **Écrivez des tests high gear dès le début.** Ils sont votre filet de sécurité pour le refactoring. Les tests low gear viendront naturellement quand vous développerez la logique métier.

---

## Ressources pour aller plus loin

- **Architecture Patterns with Python** (Harry Percival & Bob Gregory) -- [cosmicpython.com](https://www.cosmicpython.com/) -- le livre original qui a inspiré ce guide.
- **Domain-Driven Design** (Eric Evans) -- le livre fondateur du DDD.
- **Patterns of Enterprise Application Architecture** (Martin Fowler) -- la référence pour les patterns Repository, Unit of Work, Service Layer.
- **Implementing Domain-Driven Design** (Vaughn Vernon) -- un guide pratique pour appliquer le DDD.
- **Clean Architecture** (Robert C. Martin) -- une perspective complémentaire sur l'isolation du domaine.

---

!!! quote "Le mot de la fin"
    L'architecture n'est pas une fin en soi. C'est un outil au service de la maintenabilité, de la testabilité et de l'évolutivité. Le meilleur code est celui qui résout le problème du jour avec la complexité juste nécessaire -- ni plus, ni moins.
