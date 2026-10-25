# R-01 — Rapport d'exécution négative

**Statut :** campagne locale terminée ; **Production HOLD maintenu**.  
**SUT :** G4-A au commit `9d8bf1c462589ac497e3e03ece48d1d9ab0b9db3`.  
**Branche de travail :** `agent-2/r01-privileged-writer-boundary`, créée au SHA exact ; aucun fichier d'implémentation modifié. PR #73, REG-ARCH #49/#50 et CB-05 n'ont pas été modifiés ni exécutés.

## Résumé des résultats

| Vecteur | Résultat sécurité | Observation réelle |
|---|---|---|
| R01-N01 — SQL ordinaire UPDATE | **PASS (portée étroite)** | Connexion SQLite ordinaire sans fonction UDF : UPDATE de `resources.reserved` refusé ; empreinte du fichier et snapshot des cinq tables contrôlées inchangés. |
| R01-N02 — SQL ordinaire current/outbox | **PASS (portée étroite)** | UPDATE `g4a_current` et INSERT `g4a_outbox` refusés ; empreinte et snapshot inchangés. |
| R01-N03 — UDF `tenir_g4a_write` forgée | **FAIL — contre-exemple confirmé** | La connexion privilégiée a enregistré une UDF renvoyant 1 ; `UPDATE resources SET reserved='19'` a été validé. Observation durable : `reserved` passe de `20.0` à `19`. La réouverture a ensuite rejeté l'état. Le rejet au redémarrage est une détection après coup, pas une prévention/annulation de l'écriture. |
| R01-N04 — DROP TRIGGER privilégié | **FAIL — contre-exemple confirmé** | `DROP TRIGGER g4a_guard_resources_update`, puis UPDATE privilégié, ont été commités. Réouverture rejetée ensuite. Même distinction : détection après commit ≠ prévention. |
| R01-N05 — remplacement fichier même UID | **FAIL — frontière fichier non exclusive** | Copie identique de la base, puis `os.replace` par le même UID `1000`, ont réussi. La réouverture a accepté la copie identique. Le test démontre la capacité de remplacement, non une falsification du contenu dans ce vecteur. Il suffit toutefois à réfuter l'exclusivité au niveau fichier prévue par le contrat. |
| R01-N06 — inventaire indépendant des writers | **INCONCLUSIVE / HOLD** | Scan statique indépendant repère cinq modules avec chemins/capacités d'écriture SQLite. Aucun artefact d'inventaire déclaré correspondant n'a été trouvé dans `docs/g4a`. Un scan statique n'établit pas l'exhaustivité des writers à l'exécution ; aucun claim de complétude n'est admissible. |

Les « 6 passed » de pytest signifient que le harnais a correctement observé/asserté ces comportements (y compris les contre-exemples). Ce n'est **pas** six tests de sécurité PASS. Les statuts ci-dessus sont les verdicts sécurité.

## Environnement et identité observés

- Linux : `a540c710f5a3`, noyau `6.18.38+`, architecture `aarch64`.
- Python système : `3.12.3`; SQLite lié au Python système : `3.45.1`.
- Venv jetable : Python `3.12.3`, pytest `9.1.1`, pluggy `1.7.0`.
- Processus de campagne : PID observé `1599`, UID/GID `1000:1000` (`ubuntu`). Tous les acteurs testés utilisent le même UID ; aucune séparation OS indépendante n'a été démontrée.
- DB temporaire : propriétaire `1000:1000`, mode `0644`; répertoire DB : `1000:1000`, mode `0755`. Cela autorise l'écriture par le propriétaire du processus et confirme que le labo n'isole pas un writer hostile de même UID.
- SQLite local : la connexion peut enregistrer une fonction UDF ; l'acteur de test peut exécuter DDL et remplacer le fichier DB. Les scénarios n'ont pas démontré de contrôle distinct des sidecars WAL/SHM, des ACL POSIX, ni des privilèges d'administration/récupération sous une identité différente.
- Stockage de test : bases jetables créées via la fixture G4-A et pytest `tmp_path`; aucune donnée de production impliquée.

Les détails de plateforme et les sorties brutes sont dans `evidence/g4a-r01/`. La commande de reproduction, depuis la racine du dépôt :

```sh
python -m pip install -e '.[test]'
python -m pytest -s -q tests/test_r01_privileged_writer_boundary.py \
  --junitxml=evidence/g4a-r01/junit-campaign.xml
```

Le fichier JUnit indique les assertions du harnais ; `raw-campaign.txt` contient les observations imprimées. Le manifeste SHA-256 fige les artefacts listés. Une nouvelle exécution génère évidemment d'autres chemins temporaires/PID ; les conclusions reproduisibles sont les comportements, pas ces valeurs volatiles.

## Frontière d'autorité réellement démontrée

La barrière SQL/trigger est efficace contre les deux mutations exécutées par un client SQL ordinaire sans UDF. Elle n'exclut pas un acteur ayant les mêmes droits OS et la capacité d'enregistrer une fonction SQLite, de modifier le schéma ou de remplacer le fichier. Le mécanisme de réouverture détecte certains états corrompus, mais après qu'une mutation privilégiée a déjà été commitée. N05 montre en plus qu'une base substituée identique peut être rouverte, donc qu'aucun contrôle d'identité externe au contenu observé n'est établi ici. L'inventaire de writers n'est pas déclaré et demeure incomplet/non vérifiable.

Cette campagne ne démontre ni CB-01 exhaustif, ni une isolation OS contre un attaquant privilégié, ni une exclusivité globale des writers, ni la sûreté de production. Les exclusions de menaces ne sont pas adoptées rétroactivement pour transformer les échecs en OUT_OF_SCOPE.

## Décision / suite requise

R-01 reste **FAIL pour toute revendication d'exclusivité face à un writer privilégié**, avec N06 en HOLD. La suite nécessite une frontière imposée par l'OS (identités distinctes, permissions du fichier **et du répertoire**, contrôles DDL/UDF et remplacement), un inventaire indépendant des writers et des tests de récupération/admin autorisée, puis une revue indépendante. Pas de merge, tag, release, dispatch réel ou promotion production autorisés par ce rapport.
