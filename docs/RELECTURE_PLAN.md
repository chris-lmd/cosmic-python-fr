# Plan de corrections — Relecture complète

## Constats et décisions

### 1. Chapitre 3 (Repository) — Lien "Prochain chapitre" incorrect
**Constat** : Le lien en fin de chapitre pointe vers le chapitre 5 (Unit of Work), sautant le chapitre 4 (Service Layer).
**Correction** : Pointer vers le chapitre 4.

### 2. Chapitre 3 — `self.événements` dans le listing de Produit
**Constat** : Dans la section "Persistence Ignorance", le code de `Produit` montre `self.événements: list[events.Event] = []` avec un renvoi au chapitre 7. Le lecteur n'a pas encore vu les events.
**Décision** : Retirer cet attribut du listing (comme au ch.2). Il sera introduit au ch.7.

### 3. Chapitre 3 — `get_par_réf_lot` non justifié
**Constat** : La méthode apparaît sans qu'on sache pourquoi chercher un produit par référence de lot.
**Correction** : Ajouter une note expliquant que cette méthode sera utilisée plus tard (modification de quantité de lot par un système externe).

### 4. Chapitre 3 — `seen` introduit sans contexte suffisant
**Constat** : L'attribut `seen` est expliqué comme "crucial pour le UoW" mais le lecteur n'a pas encore vu le UoW.
**Correction** : Reformuler pour être plus honnête : "Cet attribut n'a pas d'utilité visible pour l'instant — il prendra tout son sens au chapitre 5."

### 5. Chapitre 7 (Events) — `allouer()` retourne `""` au lieu de lever une exception
**Constat** : Au ch.1 et ch.2, `allouer()` lève `RuptureDeStock`. Au ch.7, elle émet un event et retourne `""`. Ce changement de contrat n'est jamais expliqué.
**Décision** : Ajouter un paragraphe expliquant la transition : la méthode ne lève plus d'exception, elle émet un event à la place. Les conséquences de la rupture sont ainsi découplées via les events.

### 6. Chapitre 7 — Handlers prématurés (`réallouer` et `ajouter_allocation_vue`)
**Constat** : Ces handlers utilisent des concepts pas encore vus (`commands.Allouer` du ch.8, `uow.session` et read model du ch.12).
**Décision** : Retirer ces handlers du ch.7. Ne garder que `envoyer_notification_rupture_stock` comme illustration (il n'utilise que `AbstractNotifications`, vu au ch.6). La table de routage en fin de chapitre peut rester comme aperçu de ce qui viendra, sans montrer le code des handlers retirés.

### 7. Chapitres 8-9-10 — Ordre des chapitres (swap Bus et Bootstrap)
**Constat** : Le ch.8 (Commands) renvoie au ch.10 (Bus) en sautant le ch.9 (Bootstrap). L'ordre logique après "Commands vs Events" est d'expliquer le mécanisme de distribution (Bus), puis l'assemblage (Bootstrap).
**Décision** : Swap les chapitres :
- `chapitre_09_bootstrap_di.md` → `chapitre_10_bootstrap_di.md`
- `chapitre_10_message_bus.md` → `chapitre_09_message_bus.md`

Mettre à jour tous les liens croisés dans l'ensemble des chapitres.

Navigation corrigée :
- Ch.7 → Ch.8 (déjà OK)
- Ch.8 → Ch.9 (Bus, ex-ch.10)
- Ch.9 (Bus) → Ch.10 (Bootstrap, ex-ch.9)
- Ch.10 (Bootstrap) → Ch.11 (TDD)

### 8. Dictionnaires de routage — Redondance
**Constat** : `EVENT_HANDLERS` et `COMMAND_HANDLERS` sont montrés en entier au ch.8, ch.9 et ch.10 (3 fois).
**Décision** : Les montrer en entier au ch.8 (illustration de la distinction command/event) et au ch.10/bootstrap (leur emplacement réel dans le code). Au ch.9/bus, faire un rappel léger sans re-lister les dictionnaires en entier.

### 9. Chapitre 7 + 13 — Évolution du handler `publier_événement_allocation`
**Constat** : Au ch.7 il fait un `logger.info`, au ch.13 il publie sur Redis. Le changement de signature n'est jamais signalé.
**Décision** :
- Au ch.7 : ajouter un commentaire/note indiquant que c'est un placeholder (en production, publierait vers un broker — voir ch.13).
- Au ch.13 : ajouter un paragraphe de transition expliquant l'évolution du handler.

### 10. Chapitre 12 (CQRS) — Bootstrap incomplet
**Constat** : Le bootstrap affiché montre `events.Désalloué: [handlers.réallouer]` sans `handlers.supprimer_allocation_vue`, qui est pourtant montré aux ch.7 et ch.9.
**Correction** : Ajouter `handlers.supprimer_allocation_vue` dans la liste des handlers de `Désalloué`.

---

## Ordre d'exécution

1. Swap fichiers ch.9 ↔ ch.10 (renommage)
2. Mettre à jour les titres internes des deux fichiers swappés ("Chapitre 9" / "Chapitre 10")
3. Corriger tous les liens croisés dans TOUS les chapitres (partie1 + partie2 + index + epilogue)
4. Appliquer les corrections de contenu (points 1 à 6, 8, 9, 10)
5. Relecture finale des zones modifiées