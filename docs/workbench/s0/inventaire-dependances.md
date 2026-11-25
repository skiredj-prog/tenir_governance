# Inventaire des dépendances — interface v5.0.0 figée

Source : [`reference/TENIR_Interface_2cIronOmega_v1.html`](reference/TENIR_Interface_2cIronOmega_v1.html), commit `21434fae24c30b2e9960676f58f283ca2154caad`, SHA-256 `407d27cc97d7d472042e0ccf56e062539f742f85888b888bdfd5dad12dadc6c1`.

Cet inventaire décrit ce que **la page attend**. Il ne décrit pas le contrat du serveur 5.7.6. Les écarts sont dans [`classification-panneaux.md`](classification-panneaux.md).

La page est un seul document HTML. Elle n'a pas de script externe, pas de feuille externe, pas de police chargée, pas de module.

## Réseau et configuration

Constantes en dur, lignes 663-664 :

- API : `http://localhost:8000`
- WebSocket : `ws://localhost:8000/ws/vps`

Aucun paramètre d'URL, aucune variable d'environnement, aucun `localStorage` ni `sessionStorage`. Le seul réglage est le champ mot de passe `jwtInp` (« JWT (optional) »). S'il est vide, les appels partent sans en-tête `Authorization`. S'il est rempli, `call` ajoute `Authorization: Bearer <texte>`. Le WebSocket est ouvert sans jeton et sans sous-protocole.

Hypothèse de la page : un `r5_server.py` IRON OMEGA R5 écoute sur le port 8000 de la machine, et le navigateur peut l'appeler depuis l'origine de la page (fichier local ou autre port). La page ne prévoit pas le CORS.

## Scripts, polices, CDN

Aucun `<script src>`, aucun `<link>`, aucun `@import`, aucun `https://` de police ou de bibliothèque.

La pile de polices est locale (`--sans` : polices système ; `--mono` : `JetBrains Mono` puis repli). `JetBrains Mono` n'est pas téléchargée. Si elle est absente, le navigateur substitue.

La seule image est un chevron SVG **inline** dans le CSS du `select` (`data:image/svg+xml,...`). L'attribut `xmlns='http://www.w3.org/2000/svg'` ne déclenche pas de requête.

## API du navigateur

| API | Usage |
| --- | --- |
| `fetch` | Tous les appels HTTP, via `call` |
| `WebSocket` | Flux `/ws/vps`, reconnexion exponentielle |
| `CanvasRenderingContext2D` | Jauge S et tracé de trajectoire |
| `Blob`, `URL.createObjectURL` | Export local du journal déjà chargé (`.jsonl`) |
| `navigator.clipboard.writeText` | Copie de la signature de serment |
| `Date`, `setInterval`, `setTimeout` | Horloges d'affichage, sondage toutes les 5 s, relance WebSocket |
| `JSON.parse` / `JSON.stringify` | Messages WS, corps JSON, export |
| DOM (`getElementById`, `classList`, `createElement`) | Rendu |

Pas de service worker, pas de `eval`, pas de stockage persistant.

## Authentification supposée par la page

- Jeton facultatif, présenté comme un JWT.
- `operator_id` facultatif sur l'adjudication (`actor_ref` seulement s'il est saisi).
- `operator_id` exigé par le formulaire de serment, envoyé en paramètre de requête, sans contrôle d'identité annoncé par le texte d'avertissement du panneau (« sign any operator_id »).
- Le nonce de transition est fabriqué dans le navigateur (`nonce-<epoch ms>-<aléa>`). L'horodatage envoyé est `Math.floor(Date.now()/1000)` en chaîne, pas celui d'une signature.

## Points d'appel

Les formes ci-dessous sont celles que le JavaScript lit ou écrit. Un champ non listé est ignoré.

### `GET /api/v1/state`

Sondage, `poll`, toutes les 5 secondes, et après une transition.

Champs lus : `s_score`, `ds_de`, `d2s_de2`, `ces_state`, `membrane_decision`, `operating_mode`, `latest_hash` (chaîne, `.slice`), `epoch_id` (chaîne, `.slice`), et pour le tracé `pressure`, `velocity`, `capacity`.

Le corps n'est pas envoyé. L'échec est avalé (`catch` vide).

### `POST /api/v1/adjudicate`

Corps :

```json
{
  "raw_input": "{\"pressure\":0.5,\"velocity\":0.5,\"capacity\":0.8,\"option_space\":3.0}",
  "actor_ref": "facultatif"
}
```

`raw_input` est la chaîne JSON des curseurs, pas un objet. `option_space` vient d'un curseur de 0 à 10, pas de 0 à 1. `actor_ref` n'est présent que si le champ opérateur du cockpit est rempli.

Champs lus dans la réponse : `membrane_decision` ou `decision`, `s_score`, `ces_state`, `event_id`.

### `GET /api/v1/ledger/recent?limit=100`

La page envoie le paramètre `limit`. Elle attend soit `{ "entries": [ ... ] }`, soit un tableau.

Champs lus par entrée : `entry_type` ou `type`, `timestamp` traité comme des **secondes Unix** (`new Date(timestamp * 1000)`), `hash` ou `entry_hash`, `parent_hash`, `operator_id` ou `actor`, `ces_state`, `membrane_decision` ou `decision`.

### `GET /api/v1/ledger/verify`

Champs lus : `valid` ou, à défaut, `chain_valid` ; `entry_count` ; `error`.

### `POST /api/v1/oath/sign?operator_id=<texte>`

Pas de corps. L'identifiant est dans la requête.

Champs lus : `oath_text`, `oath_signature` ou `signature`.

### `POST /api/v1/transition`

Corps :

```json
{
  "target_mode": "SHADOW_PASSIVE",
  "operator_id": "...",
  "oath_text": "...",
  "oath_signature": "...",
  "nonce": "nonce-<ms>-<aléa>",
  "timestamp": "<secondes Unix>"
}
```

Valeurs de `target_mode` proposées par le formulaire : `SHADOW_PASSIVE`, `SHADOW`, `SHADOW_CRITICAL`, `ENFORCE`, `SHADOW_OFF`.

Champs lus : `new_mode` ou `operating_mode`, `event_id` ou `entry_id`.

### `ws://localhost:8000/ws/vps`

Ouvert sans authentification. Chaque message texte est `JSON.parse`, puis passé tel quel à la barre de posture, au tracé et au flux. La page attend un objet **plat** (les mêmes champs que `/api/v1/state`), pas une enveloppe `{ "type", "payload" }`.

### Export

`EXPORT SEAL` ne appelle aucun serveur. Il sérialise en JSONL les entrées déjà présentes dans `st.entries` et déclenche un téléchargement `tenir_audit_seal_<ms>.jsonl`.
