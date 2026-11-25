# Écarts avec la cartographie fonctionnelle

## Document cherché

Le nom indiqué pour la cartographie, `CARTOGRAPHIE-FONCTIONNELLE-TENIR-GOV-5.7.7-ec00d98.md`, **n'est pas dans ce dépôt**.

Recherche faite au moment de S0 :

- arborescence de travail et `docs/` ;
- historique Git (`git log --all` sur `*CARTOGRAPHIE*`, `*cartographie*`, `*ec00d98*`) ;
- index de code GitHub du dépôt `skiredj-prog/tenir_governance` et de l'organisation `skiredj-prog`.

Aucun fichier, aucun commit. Il n'y a pas non plus de version 5.7.7 dans `pyproject.toml`, `CHANGELOG.md` ou `.zenodo.json` : la ligne publiée citée par ces fichiers est **5.7.6**. On ne peut donc pas rapporter ici des limites « que la cartographie 5.7.7 liste », parce que ce texte n'est pas une source du dépôt.

S1 ne doit pas inventer cette cartographie, ni présenter son absence comme une certification.

## Limites écrites dans le dépôt, qu'une démo ne doit pas masquer

Les phrases entre guillemets sont celles des fichiers. Une Workbench publique en lecture seule peut dessiner une jauge. Elle ne peut pas laisser croire que ces limites sont levées.

### Le harnais REG n'est pas une certification

`CHANGELOG.md`, entrée 5.7.6, « Scope, known limits and trust boundaries » :

> REG conformance is not demonstrated. The reg-conformance harness has not been re-run on 5.7.6. Its last result on this line is the one stated in the 5.7.5 entry (`NOT_CERTIFIABLE`, from the unmerged draft skiredj-prog/reg-conformance#81; REG v0.7.0 is the normative reference).

Même fichier, entrée 5.7.5 :

> the REG harness result on this code is `NOT_CERTIFIABLE`

et le détail chiffré du brouillon de harnais : 61 vecteurs, 4 sondes d'effet, 17 contrôles de propriété ; sur le port par défaut, 10 PASS et 50 `ADAPTER_REQUIRED` parmi les vecteurs ; un échec de propriété, `PX-REG-STATE-010`. Avec le port τ_K, `CT-R4-005` est `INCONCLUSIVE`.

Le mot `INCOMPLETE` n'est pas le statut écrit dans ce changelog. Le statut écrit est `NOT_CERTIFIABLE`, et le harnais n'a pas été relancé sur 5.7.6. Une démo ne dit pas « REG conforme », ni « harnais vert ».

### Périmètre mono-hôte, FM-20 ouvert

`CHANGELOG.md`, 5.7.6, même section :

> Deployment scope. Unchanged from 5.7.5: one controlled host and one governor process [...]. Raft stays restricted to development/test loopback nodes while FM-20 is Open. Not exercised: the SQLite WAL backend under concurrent processes, and clock skew between processes.

`docs/ROADMAP_v5.7.0.md` fixe la cible de remédiation multi-hôte à 5.8.0 et rappelle que FM-20 reste ouvert. L'interface figée, avec son WebSocket « VPS » et son formulaire de mode, ressemble à un poste de commande. S1 doit dire que ce dessin n'est pas un plan de contrôle multi-hôte et n'exerce pas Raft.

### Ce que le dépôt ne dit pas, sous les mots proposés en exemple

Les formules « SDK monitoring client untested » et « kernel connectors insufficiently exercised » **ne figurent pas** dans l'arbre, ni dans la cartographie absente.

Le plus proche, à ne pas confondre avec ces formules :

- `docs/release-v5.7.0-qualification.md` : « independent connector verification remains outstanding » (vérification indépendante du connecteur de la campagne de qualification 5.7.0, pas un audit des connecteurs `tenir_kernel/connectors/` daté de 5.7.6).
- `docs/V5.7.5_HARDENING_BACKLOG.md`, ligne `DD-575-05`, statut `OPEN` : fumée d'intégration API/SDK sur une roue installée, hors édition éditable. Ce n'est pas écrit « client de monitoring non testé ».
- `docs/HOLD_MIGRATION.md` : le SDK d'habilitation HOLD n'est pas migré, et « No profile import compatibility is claimed ».
- `CHANGELOG.md`, 5.7.6, « Verification scope » : la suite citée n'a pas exercé « the SQLite WAL backend under concurrent processes, Raft, clock skew between processes ».

S1 ne transforme pas ces phrases en « les connecteurs sont insuffisamment testés » ni en « le client de monitoring est non testé », tant que ce n'est pas écrit. Elle ne fait pas non plus l'inverse : elle ne montre pas un tableau de bord comme s'il surveillait un SDK certifié.

### L'interface figée n'est pas une surface de ces limites

La page v5.0.0 ne mentionne pas REG, FM-20, le mandat, `option_space` manquant, ni l'attestation. Brancher son dessin sur le noyau 5.7.6 sans le dire masquerait exactement le changelog. S1, si elle réutilise la jauge ou la frise, affiche à côté le périmètre : illustration, pas qualification, pas `ENFORCE`, pas harnais REG.
