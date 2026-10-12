# Guide pratique — Composition deux-noyaux (v5.3.0)

**Profil :** agentic-governance-profile-v1.0.0
**Compliance :** profile-bound
**Schema :** TENIR 2.0 (1.2)

Ce guide montre comment utiliser la composition deux-noyaux : le moniteur de risque continu (`TenirKernel`) composé avec l'enforceur d'invariants discrets (`TransitionMembrane`).

---

## 1. Installation

```bash
pip install -e ".[api]"
```

Aucune dépendance supplémentaire — la composition utilise le `TenirKernel` existant.

---

## 2. Démarrage rapide

```python
from tenir_governance.sdk import DualKernelCompositionClient
from tenir_governance.objects import Agent, Passage, Resource

# 1. Créer le client
client = DualKernelCompositionClient()

# 2. Enregistrer les agents et ressources
client.register_agent(
    Agent(agent_id="alice", authority_score=0.9,
          trusted_delegates=frozenset({"bob"}))
)
client.register_resource(
    Resource(resource_id="budget", capacity=100.0)
)

# 3. Créer un passage (action à gouverner)
p = Passage(agent_id="alice", action_type="payment", amount=10.0)

# 4. Adjudiquer
result = client.adjudicate(
    p,
    pressure=0.2, velocity=0.3, capacity=1.0,
    resource_id="budget", amount=10.0,
)

print(result["composed_decision"])  # "PASS"
print(result["operation_state"])     # "reserved"
```

---

## 3. Les quatre décisions possibles

| Décision     | Posture KB3      | Sens opérationnel                          |
|--------------|------------------|---------------------------------------------|
| `PASS`       | NORMAL           | Les deux noyaux valident — autorisé         |
| `FLAG`       | CAUTION          | Risque non-bloquant — alerte               |
| `HOLD`       | HOLDING-FIRST    | Preuve manquante ou budget près de la limite|
| `HARD_VETO`  | HOLDING-FIRST    | Risque concluant ou invariant violé — bloc |

**Fail-closed :** si un noyau lève une exception, le résultat est `HOLD`, jamais `PASS`.

---

## 4. Cycle de vie complet d'une opération

L'état durable garantit l'at-most-once dispatch :

```
CREATED → RESERVED → DISPATCHING → CONFIRMED
                                → FAILED
                                → UNKNOWN
```

```python
from tenir_governance.objects import Passage

p = Passage(agent_id="alice", action_type="payment", amount=25.0)

# Étape 1 : Adjudiquer (CREATED → RESERVED si PASS)
result = client.adjudicate(
    p, pressure=0.2, velocity=0.3, capacity=1.0,
    resource_id="budget", amount=25.0,
)
assert result["composed_decision"] == "PASS"
assert result["operation_state"] == "reserved"

# Étape 2 : Dispatcher (RESERVED → DISPATCHING — claim atomique)
dispatch_result = client.dispatch(
    p.idempotency_key,
    resource_id="budget", amount=25.0,
)
assert dispatch_result["dispatched"] is True

# Étape 3a : Confirmer (DISPATCHING → CONFIRMED — l'effet a eu lieu)
client.confirm(p.idempotency_key)

# OU Étape 3b : Échouer (→ FAILED — l'effet n'a pas eu lieu, budget libéré)
client.fail(p.idempotency_key, resource_id="budget", amount=25.0)

# OU Étape 3c : Incertain (→ UNKNOWN — timeout, effet peut avoir eu lieu)
client.mark_unknown(p.idempotency_key)  # → réconciliation humaine
```

---

## 5. Ce que chaque noyau vérifie

### Noyau de scoring (TenirKernel via ScoringKernelAdapter)

| Signal                    | Seuil        | Décision    |
|---------------------------|--------------|-------------|
| S = K/(P·V+ε) ≤ hard_veto | 0.75         | HARD_VETO   |
| S ≤ tau_floor             | 0.42         | HARD_VETO   |
| S < flag_floor            | 0.90         | FLAG        |
| Irreversibility > Authority | géométrie  | HARD_VETO   |
| Evidence quality < 0.40   | géométrie    | FLAG        |
| Option space ≤ block      | 0.20         | HARD_VETO   |
| Option space ≤ alert      | 0.35         | FLAG        |

### Noyau membrane (TransitionMembrane)

| Invariant | Ce qu'il vérifie                          |
|-----------|--------------------------------------------|
| I3        | Délégation : l'agent est-il trusté ?       |
| I4        | Budget : le montant est-il dans la limite ?|
| I6        | Quarantine : l'agent est-il isolé ?        |
| I7        | Terminalité : le passage est-il terminal ? |
| I8        | Trajectoire : cohérence temporelle         |
| I9        | Contraintes temporelles                    |

**Les invariants obligatoires ne sont jamais sautés**, même avec un score sain.

---

## 6. Récupération après crash

```python
# Trouver les opérations non résolues
recovery = client.recover()
print(recovery["unresolved_count"])

for op in recovery["unresolved"]:
    if op["recommendation"] == "reconcile":
        # DISPATCHING : l'effet a pu avoir lieu — ne PAS renvoyer
        # Vérifier côté fournisseur, puis confirm() ou fail()
        pass
    elif op["recommendation"] == "human_review":
        # UNKNOWN : intervention humaine requise
        pass

# Reprendre une opération existante par sa clé
resume = client.resume_existing("op-existing-key")
if resume["action"] == "dispatch":
    # L'opération est RESERVED — safe à dispatcher
    client.dispatch(resume["idempotency_key"],
                    resume["resource_id"], resume["amount"])
elif resume["action"] == "re_adjudicate":
    # CREATED — aucun check n'a été lancé, safe à ré-adjudiquer
    pass
elif resume["action"] == "not_found":
    # Clé inconnue — utiliser adjudicate() pour une nouvelle opération
    pass
```

---

## 7. Escalade de budget

Un `FLAG` combiné avec un budget près de la limite escalade en `HOLD` :

```python
from tenir_governance.composition import CompositionPolicy

# Seuil personnalisé à 50%
client = DualKernelCompositionClient(
    policy=CompositionPolicy(budget_escalation_threshold=0.50)
)

# Premier paiement : 60€ sur budget 100€ → PASS
p1 = Passage(agent_id="alice", action_type="payment", amount=60.0)
client.adjudicate(p1, pressure=0.2, velocity=0.3, capacity=1.0,
                  resource_id="budget", amount=60.0)
client.governor.membrane.commit_passage(p1, "budget", 60.0)

# Second paiement avec stress modéré (FLAG) + budget 60% > seuil 50% → HOLD
p2 = Passage(agent_id="alice", action_type="payment", amount=5.0)
r2 = client.adjudicate(p2, pressure=0.9, velocity=0.9, capacity=0.7,
                       resource_id="budget", amount=5.0)
assert r2["composed_decision"] == "HOLD"  # escalade
```

---

## 8. Sortie conforme au schéma TENIR 2.0

Pour obtenir une sortie conforme au schéma canonique 1.2 :

```python
schema_result = client.adjudicate_schema(
    p, pressure=0.2, velocity=0.3, capacity=1.0,
    resource_id="budget", amount=10.0,
)

# Champs canoniques
print(schema_result["posture"])            # "NORMAL"
print(schema_result["execution_mode"])      # "enforce"
print(schema_result["execution_allowed"])   # True
print(schema_result["compliance_class"])    # "profile-bound"
print(schema_result["degraded_mode"])       # False

# Synthèse composite
print(schema_result["synthesis"]["final_posture"])        # "NORMAL"
print(schema_result["synthesis"]["structural_core"])      # scoring decision
print(schema_result["synthesis"]["agency_state"])        # membrane decision
print(schema_result["synthesis"]["principal_failure_mode"])  # None

# Trace
print(schema_result["trace"]["trace_id"])   # idempotency key
print(schema_result["trace"]["stages"])      # [scoring, membrane, composition, operation]

# Disclosure (versioning policy §6)
print(schema_result["disclosure"]["active_profiles"])
# ["agentic-governance-profile-v1.0.0"]
print(schema_result["disclosure"]["semantic_authority_not_held"])
# ["canonical-doctrine", "schema-definition", "posture-determination"]
```

---

## 9. Délégation

```python
from tenir_governance.objects import Delegation

# alice délègue à eve (non trusté)
p = Passage(agent_id="alice", action_type="delegation", recipient_id="eve")
delegation = Delegation(
    delegating_agent_id="alice",
    delegatee_agent_id="eve",
)

result = client.adjudicate(
    p, pressure=0.2, velocity=0.3, capacity=1.0,
    delegation=delegation,
)

# Le scoring passe (stress faible) mais la membrane véto (I7 : délégation non trustée)
assert result["scoring_decision"] == "PASS"
assert result["membrane_decision"] == "HARD_VETO"
assert result["composed_decision"] == "HARD_VETO"
assert "I7" in result["membrane_rationale"]
```

---

## 10. Duplication bloquée

La clé d'idempotence est claimée durablement avant toute évaluation.

```python
p = Passage(agent_id="alice", action_type="payment", amount=10.0,
            idempotency_key="op-unique-123")

# Premier appel : PASS
r1 = client.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                        resource_id="budget", amount=10.0)
assert r1["composed_decision"] == "PASS"

# Second appel avec la même clé : HARD_VETO (duplicate)
p2 = Passage(agent_id="alice", action_type="payment", amount=10.0,
             idempotency_key="op-unique-123")
r2 = client.adjudicate(p2, pressure=0.2, velocity=0.3, capacity=1.0,
                       resource_id="budget", amount=10.0)
assert r2["composed_decision"] == "HARD_VETO"
assert "Duplicate" in r2["composed_rationale"]
```

---

## 11. API complète

| Méthode | Rôle |
|---------|------|
| `client.adjudicate(passage, **kwargs)` | Adjudique — retourne le dict implementation-level |
| `client.adjudicate_schema(passage, **kwargs)` | Adjudique — retourne le dict conforme schema 1.2 |
| `client.dispatch(key, resource_id, amount)` | Claim atomique DISPATCHING |
| `client.confirm(key)` | DISPATCHING → CONFIRMED |
| `client.fail(key, resource_id, amount)` | → FAILED + libère/refund le budget |
| `client.mark_unknown(key)` | DISPATCHING → UNKNOWN |
| `client.resume_existing(key)` | Reprend une opération existante |
| `client.recover()` | Liste les opérations non résolues |
| `client.register_agent(agent)` | Enregistre un agent |
| `client.register_resource(resource)` | Enregistre une ressource/budget |
| `client.governor` | Accès direct au DualKernelGovernor |
| `client.membrane_state` | Accès direct à l'état de la membrane |

---

## 12. Limites garanties

| Garantie | Niveau |
|----------|--------|
| At-most-once dispatch via la gate | **Local** (in-process) |
| Composition fail-closed | **Local** |
| Invariants obligatoires non sautables | **Local** |
| Réservations budget atomiques | **Local** (threading.Lock) |
| Sérialisation inter-processus (single-host) | **Local** (fcntl.flock) |
| Coordination inter-hôtes | **Infrastructure distribuée requise** |
| Exactly-once effets externes | **Dépend du fournisseur** |
