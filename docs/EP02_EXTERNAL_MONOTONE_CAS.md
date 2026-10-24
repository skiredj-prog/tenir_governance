# EP-02 external monotone CAS protocol

Status: experimental protocol and reference service. It is not wired to the
governor and is not production approval.

`ep02_external_cas` puts an authenticated protocol boundary around one
`DurableAnchorCAS` authority. Remote clients sign every bootstrap or CAS
request. The authority verifies a pinned client key generation, performs the
atomic database operation, and signs a canonical receipt. A lookup uses a new
client-signed challenge and returns a signed `RECORDED` or `NOT_RECORDED`
observation.

The protocol adds the property that authenticated reads alone cannot provide:
all writers submit to the same compare-and-set authority. Two independent
service processes sharing that authority are tested to produce one `APPLIED`
and one durable `CONFLICT` for competing expected states.

## Deployment boundary

The module is transport neutral. A transport adapter sends the canonical JSON
bytes produced by `encode_request()` and `encode_lookup_query()` and returns
the canonical signed receipt bytes. The adapter must enforce TLS, endpoint
authentication, request size and time limits, connection limits, and operator
access policy. The included `ExternalCASClient` converts connection, socket and
timeout errors to `ExternalOutcomeUnknown`.

This reference service uses one SQLite authority. It can arbitrate clients on
different hosts when deployed behind a remote service, but it does not provide
multi-server replication, leader election, quorum, automatic failover or
Byzantine fault tolerance. Sharing the SQLite files over a network filesystem
is outside the guarantee.

## Request and receipt binding

The client signature covers:

- protocol domain and version;
- authority ID;
- client ID and key generation;
- client request ID;
- bootstrap or CAS operation;
- explicit expected checkpoint for CAS;
- the exact v2 signed successor payload, signer and signature.

The durable internal request namespace binds client ID, client key generation
and request ID. The durable request digest separately binds the operation,
authority profile, expected checkpoint and successor. Reusing an ID with a
different context fails closed.

The authority signature covers the external request digest, terminal outcome,
original observed and result checkpoints, current checkpoint, durable sequence,
replay flag and lookup challenge. Clients pin the authority public key and
verify every receipt before using it.

## Partition and retry procedure

1. Generate the request once and retain its exact bytes and request ID.
2. Submit it through `ExternalCASClient.execute()`.
3. On `ExternalOutcomeUnknown`, do not classify the operation as failed, do not
   compensate, and do not allocate a new request ID.
4. Generate a fresh challenge with `new_lookup_challenge()` and submit a signed
   lookup for the exact request.
5. A verified `RECORDED` receipt returns the original terminal result.
6. `NOT_RECORDED` is one read observation and does not prove that a concurrent
   request cannot still commit. Retry the exact original request.

## Remaining proof obligations

- A production HTTP or RPC adapter and its authentication, availability and
  denial-of-service controls are not implemented here.
- The service signing key and authorized client key generations require an
  operator-controlled provisioning and rotation process.
- Multi-server consensus and recovery from loss of the single authority remain
  external system responsibilities.
- A consistent rollback of the authority database and all pinned local trust
  material still requires an independent monotone witness to detect.
- No receipt is an authorization to perform an irreversible provider effect.
