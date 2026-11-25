# Classification des panneaux — interface v5.0.0 figée

Référence : [`reference/TENIR_Interface_2cIronOmega_v1.html`](reference/TENIR_Interface_2cIronOmega_v1.html) (SHA-256 `407d27cc97d7d472042e0ccf56e062539f742f85888b888bdfd5dad12dadc6c1`).

Trois classes, pour préparer une Workbench **en lecture seule** (S1), sans servir ce fichier.

| Classe | Sens pour S1 |
| --- | --- |
| Visuel / conceptuel | Forme, vocabulaire et dessin réutilisables sur des données figées ou fictives. Aucun appel qui change l'état du noyau. |
| Contrat R5, périmé face à 5.7.6 | La page parle à un serveur qui n'a plus ce contrat. Le paquet courant est 5.7.6 (`pyproject.toml`). 5.7.7 n'est pas déclaré dans ce dépôt. |
| Autorité | Serment, transition de mode, `ENFORCE`. Ne doit jamais partir dans une démo publique. |

Le serveur durci visé par la comparaison est `r5_hardened/IRON_OMEGA_R5/r5_server.py` sur la ligne 5.7.6 : jeton Bearer obligatoire (≥ 32 caractères), `operator_id` égal à `TENIR_OPERATOR_ID`, `runtime_object` schéma 1.2 en `ENFORCE`, `raw_input` refusé dans ce mode.

## En-tête et barre de session

Visuel réutilisable : marque `TENIR.`, onglets Cockpit / Forensics / Ceremony, pastille de connexion.

Périmé : le libellé « JWT (optional) » et l'envoi conditionnel du Bearer (`jwt`, `hdrs`, lignes 678-684). En 5.7.6 le secret n'est pas un JWT, et son absence vaut 401 sur `/api/v1/*`. Les URL `http://localhost:8000` et `ws://localhost:8000/ws/vps` sont en dur. Le WebSocket ne présente pas de jeton ; le serveur 5.7.6 ferme la socket sans Bearer (en-tête ou sous-protocole).

Ce n'est pas une commande d'autorité. C'est un accès. Une démo publique ne collecte pas ce secret.

## Cockpit — dessin

Visuel réutilisable, sans appel réseau :

- Barre de posture : libellés S, CES, membrane, mode (`updateBar` pour la mise en forme ; les seuils affichés τ 0,42, S block 0,75, S alert 0,90 sont des constantes de la page, `S_BLOCK`, `S_ALERT`, `TAU`, pas une lecture de politique).
- Jauge en arc (`drawGauge`) et pastilles de dérivées (`updateDerivs`).
- Tracé P / V / K / S (`drawTraj`, `pushPoint`) et sa légende.
- Curseurs comme **illustration** de pression, vélocité, capacité, espace d'options. Le curseur O va de 0 à 10 (défaut 3,0). Le schéma 1.2 borne `option_space` à [0, 1]. Pour une démo, l'échelle affichée doit être celle du concept qu'on montre, et étiquetée comme illustration, pas comme saisie du noyau.
- Bandeau « BACKLOG » (plan de travail, journal, BFF). Il ne appelle rien. Il dit que ces surfaces ne sont pas là. Une démo ne doit pas le transformer en promesse qu'elles existent.

`posture`, `sColor`, `drawGauge`, `drawTraj`, `relTime` sont des fonctions de présentation. Elles peuvent être réécrites dans S1. Elles ne sont pas une preuve de stabilité du système.

## Cockpit — écriture

`ADJUDICATE` (`POST /api/v1/adjudicate`) **écrit** une entrée de grand livre quand le serveur l'accepte. Ce n'est pas un changement de mode, mais ce n'est pas une lecture.

Contrat périmé : le corps est `{ raw_input: <JSON des curseurs>, actor_ref? }`. La grammaire NSL du serveur durci ne compile pas ce JSON. En `ENFORCE`, `raw_input` est refusé ; il faut un `runtime_object` TENIR 2.0 schéma 1.2. La page lit `event_id`, que cette réponse ne fournit pas (`entry_id`). Elle lit `operating_mode` sur l'état, alors que `GET /api/v1/state` du serveur durci renvoie `mode`.

S1 peut montrer un verdict **figé**. S1 n'envoie pas ce POST à un noyau.

## Forensics

Visuel réutilisable : filtre local, frise, pastilles de type / CES / décision, export d'un tableau **déjà en mémoire**. Le dessin ne prouve pas l'intégrité d'un grand livre réel.

Lecture opératoire, pas une démo publique : `LOAD ENTRIES` et `VERIFY CHAIN`. Elles exigent le jeton en 5.7.6. Le paramètre envoyé est `limit=100` ; le serveur durci lit `n`. La page traite `timestamp` comme des secondes Unix ; le grand livre durci écrit un horodatage ISO. Elle cherche `parent_hash` ; la chaîne réelle est `previous_hash`. Elle colore les types `ADJUDICATION`, `TRANSITION`, `OATH`, `GENESIS` ; les entrées durcies sont `observation` et `control_transition`.

`VERIFY CHAIN` affiche `valid` et `entry_count`. Ce n'est pas une certification. Une démo qui montrerait « Chain valid » sur un jeu d'essai doit dire que le jeu est synthétique.

## Ceremony — autorité, exclue de toute démo publique

Tout le panneau l'est : `REQUEST OATH`, `COPY`, `REGENERATE`, `SUBMIT TRANSITION`, les champs de serment, de signature, de nonce et de mode, y compris `ENFORCE` et `SHADOW_CRITICAL`.

Écarts qui rendent le panneau faux contre 5.7.6, en plus de l'interdiction de le publier :

- Le texte d'avertissement dit que n'importe quel `operator_id` est signé. Le serveur durci répond 403 si l'identifiant n'est pas `TENIR_OPERATOR_ID`.
- L'appel est `POST /api/v1/oath/sign?operator_id=`. Le serveur durci attend un corps JSON `{ "operator_id" }`.
- La signature du serveur couvre le nonce et l'horodatage **qu'il renvoie**. La page ignore ces deux champs, fabrique un autre nonce, et envoie l'heure Unix locale. La vérification échoue.
- Le mode `SHADOW` est dans la liste. Le serveur durci ne l'accepte pas.
- `ENFORCE` n'est accepté qu'après une transition enregistrée vers `SHADOW_CRITICAL`. La page ne le dit pas.

Aucune de ces commandes, ni une version « désactivée mais encore dans le DOM avec son texte de serment opérationnel », ne va dans S1. Le vocabulaire de mode peut être expliqué dans une note, pas offert comme bouton.

## Fonctions, en une ligne

| Fonction | Classe |
| --- | --- |
| `posture`, `sColor`, `drawGauge`, `updateDerivs`, `drawTraj`, `relTime` | Visuel |
| `updateBar`, `pushPoint` | Visuel, lié aux champs périmés ci-dessus |
| `addEvent`, `renderTL` | Visuel, avec injection HTML (note ci-dessous) |
| `call`, `poll`, `connectWS`, `schedRetry` | Accès réseau, contrat R5 périmé |
| Clic `adjBtn` | Écriture de grand livre, contrat périmé |
| `btnLoad`, `btnVerify`, `btnExport`, `fSearch` | Lecture ou export local ; formes de réponse périmées |
| `mkNonce`, `btnOath`, `btnCopySig`, `btnNonce`, `btnTrans` | Autorité. Interdit en démo publique |

## Note de sécurité pour une revue ultérieure — HTML construit depuis les réponses

La page injecte des chaînes venant du réseau, ou d'une erreur dérivée de ces réponses, dans `innerHTML`. Ce n'est pas corrigé ici. S1 ne reprend pas ces patrons.

| Endroit | Lignes du fichier figé | Ce qui est interpolé |
| --- | --- | --- |
| `addEvent` | 945-951 | `ces_state` (ou le type), `membrane_decision`, préfixe de `hash` / `entry_hash`, et une couleur tirée de `ces_state` dans un attribut `style` |
| Erreur d'adjudication | 1030 | `e.message` |
| Erreur de chargement du journal | 1045 | `e.message` |
| `renderTL`, ligne vide | 1083 | Texte fixe |
| `renderTL`, chaque entrée | 1102-1121 | `entry_type` / `type`, `ces_state`, `membrane_decision` / `decision`, `operator_id` / `actor`, préfixes de hash, et `JSON.stringify(entry)` dans un `<pre>`. `JSON.stringify` n'échappe pas `<`. Un champ texte du serveur peut sortir du `<pre>`. |
| Serment, validation locale | 1150 | Texte fixe |
| Serment, succès | 1168 | Texte fixe |
| Serment, erreur | 1170 | `e.message` |
| Transition, succès | 1194-1198 | `new_mode` / `operating_mode`, `event_id` / `entry_id` |
| Transition, erreur | 1202 | `e.message` |

`call` met `detail` du serveur dans `Error.message` (ligne 703-706), y compris un objet JSON. Les chemins d'erreur ci-dessus réinjectent donc le corps d'erreur. Les chemins `textContent` (jauge, libellé de chaîne, champ de signature) ne parsent pas le HTML ; ils ne sont pas dans ce tableau.
