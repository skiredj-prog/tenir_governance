# TENIR-Gov v5.7.0 — Backlog et plan de sprints

**Statut :** document de planification historique (baseline de travaux), non preuve de l'état courant ; vérifier le code, les CI et les décisions fusionnées sur le SHA ciblé. Revue Harv indépendante non présumée.
**Périmètre :** les trois blocs bloquants définis pour v5.7.0
**Règle de livraison :** aucune fusion ni publication avant les gates de release ci-dessous.

## Objectif de release

Livrer un noyau **mono-host à garanties explicitement bornées** : corriger les quatre défauts P0 I8/déterminisme/validation/persistance, couvrir I5 et I2, puis fournir les preuves, le manifeste et le rapport de revue indépendante. FM-20 reste **Open** dans cette version. Le démarrage multi-host et Raft en production sont bloqués jusqu'à la cible 5.8.0.

Le code actuellement observé sur les branches de travail ne compte comme terminé qu'après intégration dans le candidat de release, tests reproductibles et revue Harv. Les PR de reproduction restent des preuves ; elles ne sont pas des correctifs à fusionner.

## Backlog priorisé

| ID | Priorité | Livrable | Critères d'acceptation | Dépend de |
|---|---|---|---|---|
| P0-1 / F-01 | P0 | Interdire `RELEASED → COMMITTED` dans `reconcile_commit()`. Représenter un effet tardif confirmé par un incident et une transaction de compensation idempotente, sans réécrire l'état historique de la réservation. | Test sur v5.6.0 démontre le défaut ; test candidat démontre état `RELEASED` inchangé, incident traçable et compensation exactement une fois ; conservation I8 vérifiée après redémarrage. | — |
| P0-2 / F-02a | P0 | Rendre toutes les commandes `@replicated` déterministes. Fournir IDs et temps côté client ; employer une horloge logique si l'ordre répliqué suffit. | Inventaire des méthodes répliquées ; aucune lecture d'horloge ni génération aléatoire/UUID dans leur corps ; replay et comparaison d'état sur nœuds concordants ; suites Raft vertes. | — |
| P0-3 / F-02b | P0 | Valider les montants dans `BudgetCoordinator`, `TransitionMembrane` et backends (single-host et Raft). | Les frontières rejettent bool, non-numérique, `NaN`, ±infini, zéro et négatif ; les rejets n'altèrent ni budget ni réservation. | — |
| P0-4 / F-02c | P0 | Remplacer le protocole multi-fichier `budget.json`/`reservations.json` par une transaction atomique unique (SQLite WAL ou équivalent formellement justifié). | Tests d'injection de crash avant/pendant/après commit, reprise après redémarrage, unicité cross-process et conservation I8 ; aucune divergence silencieuse entre registre et compteurs. | P0-1, choix d'architecture |
| TM-1 / FM-05 / I5 | P1 | En cas de provider `FAILED` et signal S3 `HYPOTHESIS_VIOLATED`, produire `HOLD` et conserver les deux éléments de preuve. | Test de conflit échoue sur 5.6.0 et passe sur candidat ; aucun chemin ne convertit ce conflit en échec terminal automatique. | — |
| TM-2 / FM-02 / I2 | P1 | Binder le permit au hash canonique du payload autorisé et contrôler le payload réel au dispatch. | Même payload canonique accepté ; altération, champs supplémentaires non autorisés, ambiguïté de sérialisation ou permit d'une autre opération refusés avant appel provider ; hash inclus dans la trace d'audit. | — |
| TM-3 / FM-20 | P0 containment | Conserver le risque ouvert et empêcher son exposition en v5.7.0 : refuser `HOST_SCOPE=multi-host`, interdire Raft dans un manifeste de production et faire appliquer cette règle par tous les chemins de démarrage. | Tests du vérificateur, du CLI, de l'API et du constructeur Raft ; chaque rejet cite FM-20. La suppression de pickle, le transport borné et sa preuve de sécurité sont reportés à 5.8.0. | Validation du garde-fou et de la politique de manifeste |
| PROOF-1 | P2 | Construire la suite de régression `test_p0_*.py` et `test_i5_*.py`, avec démonstration du défaut sur 5.6.0. | Matrice de preuve par ID ; chaque test ciblé échoue sur tag v5.6.0 pour la raison attendue et passe sur le candidat ; assertions de sécurité non affaiblies. | P0-1 à P0-4, TM-1 à TM-3 |
| PROOF-2 | P2 | Implémenter le schéma `deployment.manifest` et son vérificateur. | Validation de manifeste valide/invalide ; champs, versions, empreintes, modes de sécurité et dépendances vérifiés ; échec fermé avant déploiement si invalide. | Décisions de release et packaging |
| PROOF-3 | P2 | Mettre à jour `threat-model-v1.md` et le rapport Harv. | I2, I5 et durabilité passent à « Covered » uniquement avec références vers code et tests verts ; risques résiduels et limites de Raft explicités ; validation Harv consignée. | TM-1 à TM-3, PROOF-1 |
| INT-1 | P0 | Intégrer le correctif local de course sur métadonnées HOLD dans la branche candidate et préserver le correctif d'expiration τ_K / ownership. | Tests de course et cycle HOLD verts dans le candidat ; revue du diff et Raft vert ; aucun drapeau ou reçu modifié si la transition durable échoue. | Revue PR #17 et #18 |

## Plan de sprints

Les sprints sont séquentiels par dépendance. Durées indicatives : une semaine par sprint ; elles sont à confirmer par Harv selon disponibilité. Les issues détaillées peuvent être créées à partir des IDs de backlog ci-dessus.

### Sprint 0 — Baseline et conception (2–3 jours)

**But :** établir une base fiable v5.6.0 et figer les décisions d'architecture avant les changements P0.

- Geler le SHA/tag candidat de référence et recenser les branches/PR ouvertes, en séparant preuves de défaut et correctifs.
- Ajouter/valider les tests de reproduction P0 et I5 sur v5.6.0 sans modifier leurs assertions.
- Faire la revue de base/diff des PR #18 et #19 ; #19 reste une preuve uniquement.
- Décider stockage atomique (SQLite WAL recommandé pour transaction unique) et stratégie transport/authentification Raft.
- Définir sérialisation canonique et contrat de permit pour I2.

**Gate S0 :** chaque backlog item a un propriétaire, une preuve de défaut, un plan de migration et une stratégie de rollback ; Harv valide les choix qui changent le stockage ou le protocole réseau.

### Sprint 1 — I8 et commandes déterministes (P0)

**Items :** P0-1, P0-2, P0-3, INT-1.

- Fermer la transition interdite `RELEASED → COMMITTED` et spécifier la compensation.
- Auditer chaque méthode `@replicated` et enlever effets non déterministes de l'état répliqué.
- Appliquer les contrôles de montant aux points d'entrée et aux implémentations mémoire/durables.
- Intégrer et revoir le correctif de course métadonnées HOLD dans le candidat ; vérifier τ_K et ownership.

**Gate S1 :** régressions P0 ciblées vertes, comparaison d'état Raft reproductible, invariants I8 conservés ; pas de faille nouvelle dans les tests HOLD.

### Sprint 2 — Transaction durable et reprise après panne (P0)

**Items :** P0-4 et compléments de preuve P0-1.

- Migrer les écritures coordonnées vers SQLite WAL (ou implémenter l'alternative approuvée).
- Définir transaction/idempotency key, migration des fichiers existants et reprise des journaux incomplets.
- Injecter des crashes à chaque frontière d'écriture ; couvrir deux processus concurrents et redémarrage.
- Documenter la procédure de sauvegarde, migration, rollback et récupération manuelle.

**Gate S2 :** aucune divergence comptable dans la campagne de crash ; migration testée depuis l'état v5.6.0 ; aucune perte ni double libération/commit.

### Sprint 3 — I5, I2 et confinement FM-20 (P1)

**Items :** TM-1, TM-2, TM-3 (confinement seulement).

- Implémenter la composition I5 fail-closed vers HOLD.
- Lier le hash canonique du payload au permit et au dispatch provider.
- Refuser le mode multi-host en v5.7.0 et Raft dans tout manifeste de production ; garder `production_allowed: false`.
- Ajouter les tests du garde-fou et documenter FM-20 comme risque résiduel ouvert, cible 5.8.0.

**Gate S3 :** tests I5/I2 et tests de blocage FM-20 verts ; aucune action provider quand preuve ou payload ne correspond. Le risque FM-20 demeure Open : ce gate ne vaut pas preuve de transport sûr.

### Sprint 4 — Preuves, manifeste et release candidate (P2)

**Items :** PROOF-1, PROOF-2, PROOF-3.

- Exécuter les tests sur le tag v5.6.0 et le candidat ; publier la matrice échec-attendu / succès-attendu.
- Ajouter le validateur de `deployment.manifest` et le brancher au pipeline de packaging/déploiement.
- Mettre à jour le threat model, les limites garanties et le rapport Harv avec les références de preuve.
- Exécuter suite complète, CI, gates et campagne Raft ; revue des changements et manifeste signé/empreint selon le mécanisme retenu.

**Gate S4 / release :** tous les items mono-host in-scope terminés ; tests complets et suite Raft exécutée ; manifeste v5.7 validé et refus multi-host prouvé ; limites FM-20 affichées ; revue indépendante et validation explicite de Harv. Cela ne constitue pas une certification et ne rend pas Raft apte à la production.

## Dépendances et ordre critique

```text
S0 architecture + baseline
  ├── S1 I8 / determinisme / validation ── S2 transaction durable ──┐
  └── S3 I5 / payload binding / FM-20 confinement ──────────────────┤
                                                                    └── S4 preuves + manifeste + rapport Harv → release
```

Le correctif de métadonnées HOLD (INT-1) s'intègre en S1, après revue de la base et du diff de la PR #18. Ne pas empiler automatiquement les branches #15/#16/#17 sur la branche durable sans campagne de régression dédiée.

## Hors périmètre v5.7.0

- Connecteurs/frameworks supplémentaires au-delà de la surface kernel déjà en cours ; le connecteur générique Python + HTTP relève du chantier kernel séparé.
- Fonctions nouvelles de l'interface, de Neo4j ou de R4/R5 sans lien direct avec les dix livrables de cette release.
- Remplacement sécurisé du transport pickle et disponibilité multi-host de production : FM-20 reste ouvert jusqu'à 5.8.0.
- Tolérance byzantine : Raft couvre les pannes crash-stop, pas les nœuds malveillants.

## Tableau de suivi de release

À mettre à jour à chaque revue de sprint :

| Bloc | État initial de l'audit | Preuve exigée pour clôture | Validateur |
|---|---|---|---|
| Fixs P0 | Partiel ; F-01 et déterminisme présents sur branche durable, validation/atomicité à compléter | Tests P0, crash campaign, CI et Raft | Harv |
| Threat model | I2/I5 couverts dans le candidat sous réserve de preuve ; FM-20 reste Open et production_allowed=false | Tests adversariaux I2/I5 ; tests du kill-switch ; revue indépendante | Harv / sécurité |
| Preuve et audit | Tests incomplets ; manifeste et rapport spécifiques à produire | Matrice 5.6.0 vs candidat, manifest validator, rapport | Harv |

## État de consolidation — 20 octobre 2026

- Candidat local : `codex/v5.7.0-rc`, basé sur `32c3b7a`. Le delta code/tests de PR #18 est réappliqué une seule fois. L'ancêtre de test seul `8f91835` et `tests/test_agent_c_pr13_hold_ownership.py` ne sont pas inclus.
- Cette roadmap n'est pas le registre de tests. Le rapport de préparation de release porte le SHA exact, les commandes exécutées, les résultats, les skips et les limites de l'environnement.
- Les constats de revue PR #18 relevés pendant l'audit sont suivis dans cette consolidation : ajout du remboursement Raft idempotent, règlement HOLD avant transition terminale, contrôle du registre durable avant dispatch, et métadonnées HOLD persistées dans la transition.
- FM-20 reste **Open**. `production_allowed` est `false`; le manifeste v5.7 et les chemins de démarrage doivent refuser le multi-host et Raft en production. La mitigation n'est pas un remplacement de pickle ; cible de remédiation 5.8.0.
- Gates restantes : CI distante sur le SHA exact, campagne élargie de crash-recovery, revue indépendante intégrale et revue Harv. Aucun merge, tag, publication PyPI ni release Zenodo n'est effectué ici.
