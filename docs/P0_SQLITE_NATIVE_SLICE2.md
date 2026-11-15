# P0 #67 — Slice 2: attestation Ed25519 liée à l'opération native SQLite

**Statut :** expérimental, `HOLD`. Cette tranche dépend de la PR #69 ; elle n'est pas un chemin d'exécution en production.

`ExperimentalNativeAttemptAuthority` ajoute une tentative `UNKNOWN` et un journal append-only, avec clé étrangère vers la vraie opération TENIR `OperationRecord` persistée par Slice 1 dans la même base que la réservation I8. L'empreinte signée est le SHA-256 du record natif canonique, lu sous la transaction SQLite. L'éditeur, la fraîcheur, la révocation, l'ID de tentative, le LEI et le nonce sont vérifiés par Ed25519.

Un reçu authentifié `EFFECT_CONFIRMED` transforme **seulement** l'état de tentative en `RESOLVED`; `NON_EXECUTION_CONFIRMED` le transforme en `FAILED`. Le `OperationRecord.state` et le budget restent `RESERVED`. Un timeout conserve `UNKNOWN`. La tentative est unique pour l'opération et le LEI : pas de T8. Les conflits et replays sont sérialisés par le même backend SQLite ; des triggers empêchent les réécritures terminales et des événements.

Le budget n'est pas réglé et le fournisseur n'est jamais appelé. Tout résultat a `decision=HOLD`, `execution_authorized=false`, `budget_settlement_authorized=false`. Une signature authentifie un émetteur et non la vérité matérielle d'un effet externe.

## Gate d'intégration G3 indispensable

**Bloqueur obligatoire avant toute bascule :** deux propriétaires concurrents — `DurableOperationRegistry` JSON et autorité SQLite — doivent tenter simultanément une transition pour la même identité d'opération. Un test adversarial doit prouver qu'ils ne peuvent pas tous deux être autorisés, y compris en présence d'instances préexistantes, crash/restart et divergence d'epoch. Les anciens binaires non participants demeurent hors de la garantie : leur présence doit bloquer une bascule de production. Aucune simple double lecture ou cohérence a posteriori ne suffit.

Les PR restent Draft, sans fusion, publication ni promotion REG/REG+.
