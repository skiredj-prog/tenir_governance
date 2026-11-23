# Guide pratique — Composition deux-noyaux (v5.7.6)

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
from tenir_governance.composition import CompositionPolicy
from tenir_governance.operations import HmacAttestationVerifier

# 1. Créer le client. Depuis 5.7.6, deux déclarations du déploiement :
#  - les conditions sous lesquelles un FLAG autorise (RFC-0 §4) ; sans elles,
#    un FLAG est tenu (HOLD), pas admis ;
#  - la clé avec laquelle le composant de réconciliation signe ses attestations
#    de non-exécution ; sans vérificateur, aucune attestation d'appelant ne vaut preuve.
OPS_SECRET = b"ops-reconciliation-key-0123456789"   # 16 octets minimum, tenu secret
client = DualKernelCompositionClient(
    policy=CompositionPolicy(flag_conditions=("revue humaine de l'effet sous 24 h",)),
    attestation_verifier=HmacAttestationVerifier({"ops": OPS_SECRET}),
)

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
    authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9,
    resource_id="budget", amount=10.0,
)

print(result["composed_decision"])  # "FLAG" : quatre axes ne sont jamais mesurés
print(result["operation_state"])     # "reserved"
print(result["declared_unmeasured"]) # les axes non mesurés, déclarés
print(result["flag_conditions"])     # ["revue humaine de l'effet sous 24 h"] : la condition déclarée
assert result["composed_decision"] == "FLAG"
assert result["flag_conditions"] == ["revue humaine de l'effet sous 24 h"]
```

Depuis 5.7.5, aucun axe non mesuré ne reçoit de valeur favorable. Un appel sans `authority`, `context_fit` ou `evidence_quality` (ou sans `pressure`, `velocity`, `capacity`) n'est pas admis. Depuis 5.7.7 (candidat non publié ; règle A du 23 novembre 2026, en attente de ratification), il est tenu (`HOLD`, réservation conservée), comme une mesure invalide, au lieu d'être refusé (`HARD_VETO` en 5.7.5 et 5.7.6) : l'absence d'une mesure n'est ni la preuve d'une violation ni celle d'une admissibilité. Un veto concluant que les mesures valides établissent seules garde la priorité (par exemple `authority=0.0`, qui est une mesure). Sans `τ_K` déclaré par la politique, ce HOLD et sa réservation restent ouverts jusqu'à leur résolution :

```python
p_missing = Passage(agent_id="alice", action_type="payment", amount=10.0)
r = client.adjudicate(p_missing, pressure=0.2, velocity=0.3, capacity=1.0,
                      option_space=0.9, resource_id="budget", amount=10.0)
assert r["composed_decision"] == "HOLD"
assert "absent measure(s) authority" in r["scoring_rationale"]
assert "authority" in r["declared_unmeasured"]
```

Depuis 5.7.6, `option_space` compte aussi : sans lui, la preuve est insuffisante et l'opération est tenue (`HOLD`), jamais admise. Depuis le candidat 5.7.7, ce `HOLD` ne réserve rien : un appelant ne peut pas immobiliser un budget en omettant une mesure ; la réservation est prise à la ré-adjudication complète (`client.governor.resolve_hold`), sous I8 à ce moment :

```python
p_no_os = Passage(agent_id="alice", action_type="payment", amount=10.0)
r = client.adjudicate(p_no_os, pressure=0.2, velocity=0.3, capacity=1.0,
                      authority=0.9, context_fit=0.9, evidence_quality=0.9,
                      resource_id="budget", amount=10.0)
assert r["composed_decision"] == "HOLD" and r["operation_state"] == "hold"
assert "absent measure(s) option_space" in r["scoring_rationale"]
assert r["budget_reserved"] is False  # rien n'est réservé tant que la mesure manque
assert client.fail(p_no_os.idempotency_key, resource_id="budget", amount=10.0) is True  # clôt la tentative
```

Un FLAG n'autorise que sous des conditions explicites (RFC-0 §4). Le déploiement les déclare dans `CompositionPolicy(flag_conditions=(...))` ; le runtime les recopie sur le résultat et au grand livre, et depuis 5.7.6 les fait respecter : avec la politique par défaut (aucune condition), un FLAG compose en `HOLD` ; et le dispatch d'une opération admise sous FLAG exige l'acquittement de chaque condition (`flag_conditions_met`, §4). Le runtime n'invente aucune condition.

```python
from tenir_governance.sdk import DualKernelCompositionClient as _Client
client_default = _Client()  # politique par défaut : flag_conditions=()
client_default.register_agent(Agent(agent_id="alice", authority_score=0.9))
client_default.register_resource(Resource(resource_id="budget", capacity=100.0))
r = client_default.adjudicate(Passage(agent_id="alice", action_type="payment", amount=10.0),
                              pressure=0.2, velocity=0.3, capacity=1.0,
                              authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9,
                              resource_id="budget", amount=10.0)
assert r["scoring_decision"] == "FLAG" and r["composed_decision"] == "HOLD"
assert "FLAG states no condition" in r["composed_rationale"]
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

M = dict(pressure=0.2, velocity=0.3, capacity=1.0,
         authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9)
# L'acquittement des conditions du FLAG : qui ou quoi a établi chaque condition déclarée.
ACK = {"revue humaine de l'effet sous 24 h": "ticket OPS-12, revue par dana"}

def admit_and_dispatch(amount):
    p = Passage(agent_id="alice", action_type="payment", amount=amount)
    # Étape 1 : Adjudiquer (CREATED → RESERVED si PASS ou FLAG)
    result = client.adjudicate(p, **M, resource_id="budget", amount=amount)
    assert result["composed_decision"] == "FLAG"
    assert result["operation_state"] == "reserved"
    # Étape 2 : Dispatcher (RESERVED → DISPATCHING — claim atomique).
    # Admise sous FLAG, l'opération ne part qu'avec l'acquittement de chaque
    # condition déclarée ; il est persisté sur l'opération et inscrit au grand livre.
    refused = client.dispatch(p.idempotency_key, resource_id="budget", amount=amount)
    assert refused["dispatched"] is False and "not acknowledged" in refused["reason"]
    dispatch_result = client.dispatch(p.idempotency_key, resource_id="budget", amount=amount,
                                      flag_conditions_met=ACK)
    assert dispatch_result["dispatched"] is True
    assert client.governor.registry.get(p.idempotency_key).flag_conditions_met == ACK
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
# Après un dispatch, il faut une attestation de non-exécution, signée par le
# composant qui a observé le fournisseur avec une clé que le client connaît
# (attestation_verifier). Le runtime vérifie la signature, persiste sa copie
# vérifiée et libère le budget. Sans attestation, ou si elle ne se vérifie pas,
# fail() refuse : l'opération passe en UNKNOWN et garde son budget.
p_b = admit_and_dispatch(10.0)
from tenir_governance.operations import NonExecutionAttestation, sign_non_execution_attestation
proof = sign_non_execution_attestation(
    NonExecutionAttestation(
        idempotency_key=p_b.idempotency_key,
        source="ops-reconciliation",                  # qui atteste
        attestation="charge ch_123: never captured",  # la preuve elle-même
        attested_at="2026-11-19T06:40:00+00:00",
    ),
    "ops", OPS_SECRET,                                # la clé déclarée au client
)
assert client.fail(p_b.idempotency_key, resource_id="budget", amount=10.0,
                   non_execution_attested=proof) is True
record_b = client.governor.registry.get(p_b.idempotency_key)
assert record_b.state.value == "failed"
assert record_b.non_execution_attested["verified_by_runtime"] is True
assert record_b.non_execution_attested["verification"] == "hmac-sha256:ops"
assert budget.consumed == consumed0 + 25.0 and budget.reserved == reserved0

# Sans attestation : UNKNOWN, budget retenu.
p_b2 = admit_and_dispatch(10.0)
assert client.fail(p_b2.idempotency_key, resource_id="budget", amount=10.0) is False
assert client.governor.registry.get(p_b2.idempotency_key).state.value == "unknown"
consumed0 += 10.0  # retenu jusqu'à la réconciliation

# Attestation non signée (ou signée avec une autre clé) : refusée, UNKNOWN,
# refus inscrit au grand livre. UNKNOWN n'est pas terminal : une attestation
# vérifiée peut encore clore la tentative.
p_b3 = admit_and_dispatch(10.0)
unsigned = NonExecutionAttestation(idempotency_key=p_b3.idempotency_key, source="ops-reconciliation",
                                   attestation="no charge", attested_at="2026-11-19T06:40:00+00:00")
assert client.fail(p_b3.idempotency_key, resource_id="budget", amount=10.0,
                   non_execution_attested=unsigned) is False
assert client.governor.registry.get(p_b3.idempotency_key).state.value == "unknown"
assert any("NON_EXECUTION_ATTESTATION_REFUSED" in e.rationale for e in client.governor.ledger.entries)
assert client.fail(p_b3.idempotency_key, resource_id="budget", amount=10.0,
                   non_execution_attested=sign_non_execution_attestation(unsigned, "ops", OPS_SECRET)) is True

# 3c : Incertain (→ UNKNOWN — timeout, l'effet a pu avoir lieu)
p_c = admit_and_dispatch(5.0)
assert client.mark_unknown(p_c.idempotency_key) is True
assert client.governor.registry.get(p_c.idempotency_key).state.value == "unknown"
assert budget.consumed == consumed0 + 30.0  # compté comme consommé jusqu'à la réconciliation
```

### Le mandat est revalidé au dispatch (5.7.6)

Entre l'admission et le dispatch, le mandat peut changer : agent retiré, délégation révoquée, limite de budget abaissée, version de politique modifiée. Sur la politique par défaut (`revalidate_mandate_at_dispatch=True`), `dispatch` et `dispatch_and_execute` le revérifient avant la revendication `DISPATCHING`. Un changement refuse en fail-closed : la réservation est libérée, la tentative se termine `FAILED` avec `execution_record = "CONFIRMED_NONEXECUTION"`, le fournisseur n'est jamais appelé, le refus est au grand livre (`HARD_VETO`, `MANDATE_REVALIDATION_FAILED`).

```python
client_rv = DualKernelCompositionClient(
    policy=CompositionPolicy(flag_conditions=("revue humaine de l'effet sous 24 h",)))
client_rv.register_agent(Agent(agent_id="alice", authority_score=0.9))
client_rv.register_resource(Resource(resource_id="budget", capacity=100.0))
budget_rv = client_rv.governor.membrane.state.resources["budget"]

p_rv = Passage(agent_id="alice", action_type="payment", amount=60.0)
assert client_rv.adjudicate(p_rv, **M, resource_id="budget", amount=60.0)["operation_state"] == "reserved"
budget_rv.capacity = 50.0   # la limite est abaissée sous la réservation
out = client_rv.dispatch(p_rv.idempotency_key, resource_id="budget", amount=60.0, flag_conditions_met=ACK)
assert out["dispatched"] is False and out["mandate_revalidation_failed"] is True
assert out["execution_record"] == "CONFIRMED_NONEXECUTION" and out["operation_state"] == "failed"
assert budget_rv.reserved == 0.0 and budget_rv.consumed == 0.0

p_rv2 = Passage(agent_id="alice", action_type="payment", amount=10.0)
assert client_rv.adjudicate(p_rv2, **M, resource_id="budget", amount=10.0)["operation_state"] == "reserved"
del client_rv.governor.membrane.state.agents["alice"]   # l'agent est retiré
out = client_rv.dispatch(p_rv2.idempotency_key, resource_id="budget", amount=10.0, flag_conditions_met=ACK)
assert out["dispatched"] is False
assert out["mandate_changes"] == ["acting agent 'alice' is no longer registered"]
```

`CompositionPolicy(revalidate_mandate_at_dispatch=False)` rétablit le comportement de 5.7.5 (le mandat retiré après admission dispatche encore).


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
elif resume["action"] == "acknowledge_flag_conditions":
    # RESERVED sous FLAG : chaque condition de resume["flag_conditions"] doit être
    # acquittée au dispatch (flag_conditions_met)
    pass
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
    policy=CompositionPolicy(budget_escalation_threshold=0.50,
                             flag_conditions=("revue humaine de l'effet sous 24 h",))
)
client_50.register_agent(Agent(agent_id="alice", authority_score=0.9))
client_50.register_resource(Resource(resource_id="budget", capacity=100.0))
M = dict(pressure=0.2, velocity=0.3, capacity=1.0,
         authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9)

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
    authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9,
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
    authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9,
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
                       authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9,
                       resource_id="budget", amount=10.0)
assert r1["composed_decision"] == "FLAG"

# Second appel avec la même clé : HARD_VETO (duplicate)
p2 = Passage(agent_id="alice", action_type="payment", amount=10.0,
             idempotency_key="op-unique-123")
r2 = client.adjudicate(p2, pressure=0.2, velocity=0.3, capacity=1.0,
                       authority=0.9, context_fit=0.9, evidence_quality=0.9, option_space=0.9,
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
| `client.dispatch(key, resource_id, amount, flag_conditions_met=None)` | Claim atomique DISPATCHING. Admise sous FLAG, l'opération exige l'acquittement de chaque condition déclarée (persisté, au grand livre). Sur la politique par défaut, le mandat (agent, délégation, limite de budget, version de politique) est revalidé avant la revendication ; un changement refuse en fail-closed et libère la réservation |
| `client.confirm(key)` | DISPATCHING → CONFIRMED |
| `client.fail(key, resource_id, amount, non_execution_attested=None)` | → FAILED + libère/refund le budget. Après dispatch, exige une `NonExecutionAttestation` signée (`sign_non_execution_attestation`) que l'`attestation_verifier` du client authentifie ; la copie vérifiée est persistée avec l'opération. Sans elle, ou si elle ne se vérifie pas → UNKNOWN, budget retenu, refus au grand livre, renvoie False |
| `client.mark_unknown(key)` | DISPATCHING → UNKNOWN, budget retenu jusqu'à réconciliation |
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
