# Workbench S0 — conservation de l'interface v5.0.0

Cet enregistrement fige l'interface opérateur d'origine. Il ne la maintient pas, ne la sert pas et ne la déploie pas.

| | |
| --- | --- |
| Fichier | [`reference/TENIR_Interface_2cIronOmega_v1.html`](reference/TENIR_Interface_2cIronOmega_v1.html) |
| Commit | `21434fae24c30b2e9960676f58f283ca2154caad` |
| Message | `release: v5.0.0 IRON OMEGA R5 -- initial public release` |
| Date | 2026-07-08 13:46:21 +0100 |
| Blob Git | `1bb1cee68373046a29dedcaa3d2943873847fa26` |
| Taille | 49 643 octets |
| SHA-256 | `407d27cc97d7d472042e0ccf56e062539f742f85888b888bdfd5dad12dadc6c1` |

Le condensé est aussi dans [`reference/SHA256SUMS`](reference/SHA256SUMS). Depuis `reference/` :

```sh
sha256sum -c SHA256SUMS
```

Le contrôle doit répondre `OK`. Toute modification du HTML, y compris une fin de ligne, invalide l'enregistrement.

## Statut

Copie de référence, byte à byte, du chemin `interface/TENIR_Interface_2cIronOmega_v1.html` à ce commit. Sur `main` au moment de cet enregistrement (`940864dfa74e563dfb287fe6505147a557610da4`), le fichier de travail a encore le même blob. Il peut diverger ensuite. Cette copie-ci ne suit pas le fichier de travail.

Elle n'est pas le cockpit local branché sur le noyau 5.7.6. Ce travail, s'il est fusionné, vit dans une autre demande de tirage et modifie le fichier de travail. S0 ne le remplace pas et ne le sert pas.

## Interdit

- Ne pas servir ce fichier (`file://`, serveur statique, FastAPI, Pages, `tenirlabs.cc`, conteneur).
- Ne pas le déployer, ne pas l'exposer, ne pas en faire une démo publique.
- Ne pas le « corriger » pour le noyau courant. Un correctif appartient au cockpit de travail, pas à l'archive.
- Ne pas l'utiliser comme base exécutable de la Workbench S1. S1, plus tard, sera une surface en lecture seule, neuve, qui pourra s'inspirer des panneaux visuels décrits dans [`classification-panneaux.md`](classification-panneaux.md). Elle ne republie pas cet HTML.

## Suite documentaire

- [`inventaire-dependances.md`](inventaire-dependances.md) — ce que la page appelle, et ce qu'elle suppose.
- [`classification-panneaux.md`](classification-panneaux.md) — visuel réutilisable, contrat R5 périmé, commandes d'autorité.
- [`ecarts-cartographie.md`](ecarts-cartographie.md) — la cartographie 5.7.7 nommée n'est pas dans le dépôt ; limites écrites ailleurs qu'une démo ne doit pas masquer.
