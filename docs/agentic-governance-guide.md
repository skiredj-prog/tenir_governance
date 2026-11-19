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
    authority=0.9, context_fit=0.9, evidence_quality=0.9,
    resource_id="budget", amount=10.0,
)

print(result["composed_decision"])  # "FLAG" : quatre axes ne sont jamais mesurés
print(result["operation_state"])     # "reserved"
print(result["declared_unmeasured"]) # les axes non mesurés, déclarés
print(result["flag_conditions"])     # [] tant que la politique n'en déclare pas
```

Depuis 5.7.5, aucun axe non mesuré ne reçoit de valeur favorable. Un appel sans `authority`, `context_fit` ou `evidence_quality` (ou sans `pressure`, `velocity`, `capacity`) n'est pas admis :

```python
p_missing = Passage(agent_id="alice", action_type="payment", amount=10.0)
r = client.adjudicate(p_missing, pressure=0.2, velocity=0.3, capacity=1.0,
                      resource_id="budget", amount=10.0)
assert r["composed_decision"] == "HARD_VETO"
assert "authority missing" in r["scoring_rationale"]
assert "authority" in r["declared_unmeasured"]
```

Pour qu'un FLAG porte une condition explicite (RFC-0 §4), le déploiement la déclare dans `CompositionPolicy(flag_conditions=(...))`. Le runtime la recopie sur le résultat et au grand livre. Il ne la fait pas respecter.

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

M = dict(pressure=0.2, velocity=0.3, capacity=1.0,
         authority=0.9, context_fit=0.9, evidence_quality=0.9)

def admit_and_dispatch(amount):
    p = Passage(agent_id="alice", action_type="payment", amount=amount)
    # Étape 1 : Adjudiquer (CREATED → RESERVED si PASS ou FLAG)
    result = client.adjudicate(p, **M, resource_id="budget", amount=amount)
    assert result["composed_decision"] == "FLAG"
    assert result["operation_state"] == "reserved"
    # Étape 2 : Dispatcher (RESERVED → DISPATCHING — claim atomique)
    dispatch_result = client.dispatch(p.idempotency_key, resource_id="budget", amount=amount)
    assert dispatch_result["dispatched"] is True
    return p

budget = client.governor.membrane.state.resources["budget"]
consumed0, reserved0 = budget.consumed, budget.reserved

# Étape 3, trois issues possibles, chacune sur sa propre opération.
# 3a : Confirmer (DISPATCHING → CONFIRMED — l'effet a eu lieu)
p_a = admit_and_dispatch(25.0)
assert client.confirm(p_a.idempotency_key) is True
assert client.governor.registry.get(p_a.idempotency_key).state.value == "confirmed"
assert budget.consumed == consumed0 + 25.0

# 3b : Échouer (→ FAILED — l'effet n'a pas eu lieu, budget libéré).
# Après un dispatch, il faut la preuve de non-exécution (fournisseur ou
# réconciliation). Sans elle, fail() refuse : l'opération passe en UNKNOWN
# et garde son budget.
p_b = admit_and_dispatch(10.0)
assert client.fail(p_b.idempotency_key, resource_id="budget", amount=10.0,
                   authoritative_non_execution=True) is True
assert client.governor.registry.get(p_b.idempotency_key).state.value == "failed"
assert budget.consumed == consumed0 + 25.0 and budget.reserved == reserved0

p_b2 = admit_and_dispatch(10.0)
assert client.fail(p_b2.idempotency_key, resource_id="budget", amount=10.0) is False
assert client.governor.registry.get(p_b2.idempotency_key).state.value == "unknown"
consumed0 += 10.0  # retenu jusqu'à la réconciliation

# 3c : Incertain (→ UNKNOWN — timeout, l'effet a pu avoir lieu)
p_c = admit_and_dispatch(5.0)
assert client.mark_unknown(p_c.idempotency_key) is True
assert client.governor.registry.get(p_c.idempotency_key).state.value == "unknown"
assert budget.consumed == consumed0 + 30.0  # compté comme consommé jusqu'à la réconciliation
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

# Seuil personnalisé à 50 %, client distinct : il faut enregistrer agent et budget
client_50 = DualKernelCompositionClient(
    policy=CompositionPolicy(budget_escalation_threshold=0.50)
)
client_50.register_agent(Agent(agent_id="alice", authority_score=0.9))
client_50.register_resource(Resource(resource_id="budget", capacity=100.0))
M = dict(pressure=0.2, velocity=0.3, capacity=1.0,
         authority=0.9, context_fit=0.9, evidence_quality=0.9)

# Premier paiement : 40 € sur 100 € → 40 % < 50 %, FLAG admis
p1 = Passage(agent_id="alice", action_type="payment", amount=40.0)
r1 = client_50.adjudicate(p1, **M, resource_id="budget", amount=40.0)
assert r1["composed_decision"] == "FLAG"
assert r1["operation_state"] == "reserved"

# Second paiement : 20 € → 60 % ≥ 50 %, le FLAG du scoring escalade en HOLD
p2 = Passage(agent_id="alice", action_type="payment", amount=20.0)
r2 = client_50.adjudicate(p2, **M, resource_id="budget", amount=20.0)
assert r2["scoring_decision"] == "FLAG"
assert r2["composed_decision"] == "HOLD"  # escalade
print(r2["composed_rationale"])
# "scoring FLAG + budget utilization 60% ≥ 50%: escalated to HOLD pending evidence."
```

---

## 8. Sortie conforme au schéma TENIR 2.0

Pour obtenir une sortie conforme au schéma canonique 1.2, l'appelant fournit le contexte canonique :

```python
# Le schéma 1.2 exige des champs que l'adjudication ne produit pas
# (identité, TAU, état épistémique, profils...). L'appelant les fournit
# explicitement dans canonical_context ; sans lui, l'adaptateur refuse
# (ValueError « canonical_context is required »).
canonical_context = {
    "rubric_version": "my-rubric-1",
    "timestamp_utc": "2026-11-19T00:00:00Z",
    "identity": {"unit_name": "payments-team", "entity_class": "organization",
                 "declared_scale": "team"},
    "tau": {"tau_hypothesis": "Preserve accountable human authority.",
            "tau_integrity": "intact"},
    "epistemic": {"status": "ok", "evidence_quality": 0.9, "manipulation_risk": 0.0,
                  "drift": 0.0, "epistemic_surrender_risk": 0.0,
                  "confidence_ceiling": "high"},
    "scores": {"pressure": 0.2, "velocity": 0.3, "capacity": 1.0, "option_space": 0.8,
               "stability": 1.0, "cooperative_control_score": 1.0,
               "trajectory_risk_score": 0.0, "irreversible_pathway_materiality": 0.0},
    "collapse_flags": [],
    "active_profiles": ["agentic-governance-profile-v1.0.0"],
    "suppressed_profiles": [],
    "profile_conflicts": [],
    "narrative_distortion_summary": {},
    "irreversible_pathway_assessment": {
        "materially_affects_irreversible_pathway": False,
        "raw_data_human_verification_required": False,
        "human_verified_raw_data": False},
    "assumptions": [],
    "constraint_geometry": {"geometry_id": "tenir-2.0-reference",
                            "coordinates": {"authority": 0.9, "context_fit": 0.9,
                                            "authority_source": "role registry",
                                            "context_source": "payment policy"}},
    "trace": {"trace_id": "trace-001", "stages": []},
    "synthesis": {"structural_core": {"pressure": 0.2, "velocity": 0.3, "capacity": 1.0,
                                      "option_space": 0.8, "tau_integrity": "intact",
                                      "stability": 1.0},
                  "epistemic_state": {"status": "ok"}, "final_posture": "CAUTION",
                  "rationale": ["Caller-supplied context."], "degraded_inputs": []},
    "schema_version": "1.2",
    "degraded_mode": ["none"],
}

p = Passage(agent_id="alice", action_type="payment", amount=10.0)
schema_result = client.adjudicate_schema(
    p, pressure=0.2, velocity=0.3, capacity=1.0,
    authority=0.9, context_fit=0.9, evidence_quality=0.9,
    resource_id="budget", amount=10.0,
    canonical_context=canonical_context,
)

print(schema_result["posture"])               # "CAUTION" (FLAG composé)
print(schema_result["execution_mode"])        # "enforce"
print(schema_result["execution_allowed"])     # True
print(schema_result["execution_disposition"]) # "allow_with_alert"
print(schema_result["compliance_class"])      # "profile-bound"
print(schema_result["degraded_mode"])         # ["none"]

# Synthèse composite
print(schema_result["synthesis"]["final_posture"])  # "CAUTION"

# Trace : identifiant = clé d'idempotence, étapes scoring, membrane, composition, operation
print(schema_result["trace"]["trace_id"])     # "op-..."
print([st["stage"] for st in schema_result["trace"]["stages"]])
# ["scoring", "membrane", "composition", "operation"]

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
    authority=0.9, context_fit=0.9, evidence_quality=0.9,
    delegation=delegation,
)

# Le scoring admet sous FLAG (stress faible) mais la membrane véto (I7 : délégation non trustée)
assert result["scoring_decision"] == "FLAG"
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

# Premier appel : admis (FLAG, quatre axes jamais mesurés)
r1 = client.adjudicate(p, pressure=0.2, velocity=0.3, capacity=1.0,
                       authority=0.9, context_fit=0.9, evidence_quality=0.9,
                       resource_id="budget", amount=10.0)
assert r1["composed_decision"] == "FLAG"

# Second appel avec la même clé : HARD_VETO (duplicate)
p2 = Passage(agent_id="alice", action_type="payment", amount=10.0,
             idempotency_key="op-unique-123")
r2 = client.adjudicate(p2, pressure=0.2, velocity=0.3, capacity=1.0,
                       authority=0.9, context_fit=0.9, evidence_quality=0.9,
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
| `client.fail(key, resource_id, amount, authoritative_non_execution=False)` | → FAILED + libère/refund le budget. Après dispatch, exige la preuve de non-exécution ; sans elle → UNKNOWN, budget retenu, renvoie False |
| `client.mark_unknown(key)` | DISPATCHING → UNKNOWN, budget retenu jusqu'à réconciliation |
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
