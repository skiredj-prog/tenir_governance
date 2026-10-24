# TENIR — Notes de vision : supervision humaine, autorisation et preuve

**Statut :** notes éditoriales pour la vision et le pitch, 2026-10-24. Les phrases ci-dessous sont des formulations de travail **fournies par l'auteur du projet** ; aucune source externe ni attribution académique n'a été vérifiée. Ne pas les présenter comme citations d'une personne tierce.

## Deux formulations de conviction

> “A human bolted onto the end of a stronger machine is not a safeguard. It is a second opinion that lowers the first.”

> “Ask for the override accuracy of your human reviewers. If nobody can give it to you, you do not have human oversight. You have a signature.”

## Interprétation TENIR / REG

Une signature ne démontre pas à elle seule la compréhension, l'indépendance du jugement ou la réduction du risque. L'approbation humaine est une **autorisation avec provenance**, pas une preuve de sécurité. Pour être matériellement utile, la personne doit pouvoir consulter des preuves indépendantes, identifier précisément l'opération et refuser sans pression fonctionnelle.

Dans le parcours REG **Ambre**, l'approbation humaine est une condition nécessaire lorsqu'elle est exigée par le profil admis ; elle n'est jamais une condition suffisante d'exécution. La qualité du contrôle se mesure sur des décisions contestées et vérifiées, et non sur le taux brut de signatures.

## Discipline de communication

- **Ne pas prétendre :** “human-in-the-loop as a safety guarantee”.
- **Formulation conditionnelle :** “human-in-the-loop as an authorization mechanism with cryptographic provenance”, uniquement lorsque l'implémentation et les preuves de l'intégration le démontrent.
- **Vérification à demander :** provenance des données affichées, identité du signataire, empreinte de l'intention, contrôle indépendant du contexte et exactitude des corrections humaines.
- **Frontière :** la conception du dispositif n'établit ni certification, ni fiabilité empirique, ni garantie de sûreté des actions externes.

Références internes : REG Registre d'extériorité E2 ; `docs/IMPACT_ASSESSMENT_V5.7.0_HITL.md` ; limites v5.7.0 dans `SECURITY.md` et `docs/DEPLOYMENT_BOUNDARIES_V5.7.0.md`.
