# JW-10 P0 — anti-downgrade et contrôle d'ancrage courant externe

**Statut : candidat expérimental. Pas de promotion JW-01..10 VERIFIED, pas de fusion ni de déploiement.**
Branche empilée sur PR #101 → #98, qui conserve les preuves JW-09/JW-10 basées sur `c23f9d61c3fa03e81432d766c05fc747b904fa61`.

## 1. Anti-downgrade de la custody (dans les implémentations natives)

- Lors de toute initialisation, `IndependentWitness` inspecte sa custody persistée avant les migrations de schéma. Une clé déjà épinglée en mode `verifiable_recovery` interdit toute ouverture `verifiable_recovery=False`, **même avec la même clé injectée**. Le redémarrage avec une clé différente continue d'être refusé.
- `NativePositivePermitAuthority` examine l'empreinte Kernel persistée **avant** les mutations de schéma ou l'initialisation du Witness. Une custody épinglée ne peut être ouverte en mode legacy, avec ou sans clé.
- Une ancienne base comportant des preuves/checkpoints non qualifiés ne peut être « promue » rétroactivement au mode vérifiable par un simple premier pin. Un pin Kernel présent mais vide exige `HOLD`.
- Ces vérifications sont **locales**. Un adversaire capable de restaurer les deux bases et leurs marqueurs vers un état antérieur peut encore contourner la mémoire de la custody; l'ancrage indépendant reste nécessaire.

## 2. Vérification d'un ancrage signé courant

Le module `w2_integration/external_cold_restart_anchor.py` fournit `ExternalColdRestartAnchorGate`, qui n'émet pas d'autorisation. Son interface `CurrentAnchorReader.read_current(subject)` doit être implémentée par une **autorité séparée** garantissant l'accès à la **dernière génération monotone**. Le vérificateur:

- utilise une clé publique Ed25519 configurée indépendamment des clés Kernel et Witness;
- vérifie l'identité du sujet, l'identifiant réel de topologie, la génération minimale et la signature du checkpoint fourni par ce lecteur;
- calcule des empreintes logiques canoniques **en lecture seule** de chacun des fichiers SQLite Kernel et Witness, y compris leurs tables et enregistrements;
- refuse toute discordance avec l'ancrage courant, toute indisponibilité du lecteur, une clé inconnue, un sujet divergent et une topologie différente;
- produit uniquement `COLD_RESTART_MATCHED_ONLY`, `execution_authorized=false`, `normative_verified=false`.

**Limites impératives :** le lecteur indépendant dans les tests est une *simulation mémoire*, pas un service monotone déployé. Un vieux checkpoint **validement signé** sera accepté si le lecteur compromis ou obsolète le présente comme courant; la signature seule ne prouve pas la fraîcheur. L'écriture effective de l'ancrage et sa mise à jour atomique avec l'opération native ne sont pas implémentées. La lecture des deux fichiers n'est pas un snapshot atomique inter-bases : elle nécessite un contexte de redémarrage **quiescent**. Le vérificateur n'est pas branché au dispatch, au provider ou au commit G4.

## 3. Tests de contrôle positif et négatif

- Déclassement mode legacy avec clé inchangée ou supprimée : refus.
- État historique non qualifié ou pin corrompu : refus.
- Checkpoint courant signé par clé d'ancrage indépendante : comparaison à froid réussie sans droit d'exécution.
- Restauration conjointe Kernel/Witness alors que le lecteur conserve une génération **plus récente** : refus.
- **Contrôle négatif obligatoire :** si le lecteur retourne un ancien checkpoint signé valide après restauration conjointe, le modèle n'identifie pas la perte de fraîcheur. Ce fait doit rester documenté; aucune qualification normative ne peut en être déduite.
- Lecteur indisponible, signature altérée, mauvaise clé publique, sujet, topologie ou état des bases divergent : refus.

## Critères de sortie P0 restant bloquants

1. Connecteur à une autorité monotone **réellement externe, vivante, avec clés et contrôle d'accès distincts**, preuve de fraîcheur/non-retour-arrière et résilience à la restauration simultanée de Kernel et Witness.
2. Protocole de publication d'ancrage sûr face aux crashs entre le commit du Kernel, celui du Witness et l'acceptation par le tiers. Il faut distinguer `HOLD`, reprise idempotente et opération externe inconnue, sans promesse d'atomicité inter-domaines.
3. Gate imposé sur tous les chemins natifs, preuve indépendante contre le provider réel ou mock adversarial, tests de crash/partition et qualification de la topologie demandée `LAB-TENIR-GOV-PROD-CANDIDATE-09` (actuellement différente).

**Verdict de phase :** `JW10_LOCAL_ANTI_DOWNGRADE_SCOPED` et `EXTERNAL_ANCHOR_COLD_RESTART_CHECK_EXPERIMENTAL`; `JW01_TO_JW10_VERIFIED=HOLD`. Toujours Draft, sans fusion, release, déploiement ni REG+.
