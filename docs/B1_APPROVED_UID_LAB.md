# B1 signed-approval UID laboratory

This is an opt-in, Linux-only synthetic experiment. The UID boundary and a
valid signature do not establish that the signer's decision is correct, that
the deployed issuer was independent, or that any external provider performed
an effect. The service has no real-provider option and must not be used for
production. B1, B2, integration and production remain HOLD.

An external issuer signs the exact operation key, evaluation ID, idempotency
key, canonical command hash, audience, approval ID, nonce, issuer key ID and
validity period. The writer pins the configured Ed25519 public key in its
private SQLite store and verifies the signature. It persists an append-only
binding in the same SQLite transaction as native I8 reservation and permit
issuance. Reuse of an approval ID, nonce,
operation or idempotency key for a different signed body is refused. Exact
replay follows the existing UNKNOWN behavior. The writer must run with a private
umask and trusted paths and be isolated from workers by Linux UIDs.

## Transaction and recovery boundary

Approval binding, native permit issuance and I8 reservation commit together.
The later positive grant, native outbox creation, witness and pre-send intent
remain separate operations or transactions. A crash after admission can leave
a bound approval and reservation without an outbox; exact retry while the
approval remains valid can continue. No atomic all-or-nothing claim is made
across approval, budget, grant, witness, outbox and an external provider. After
an ambiguous send, no automatic resend or second external effect is asserted.
An orphaned approval binding left by the earlier two-transaction prototype is
refused for operator review; it is not silently converted into a new permit.

Trust-key installation is refused on a store already containing positive
operations, and a pinned key change is refused. Key rotation and revocation,
clock manipulation, administrator/same-writer-UID compromise, system service
management, source integrity, secure key custody and legacy production paths
remain unresolved. The original unapproved experimental service still exists;
the environment must not expose or start it as a production authority.

Moving to a real provider requires a trusted policy issuer, a single reviewed
integrated authority, transaction/recovery design, real credential and network
isolation, exhaustive writer and connector inventory, provider-specific
idempotency/reconciliation and independent end-to-end validation.

For the reconstructed generic laboratory provider, a valid native claim alone
is not a send right. A durable send election precedes a one-use capability held
only by the active writer process. After an ambiguous result or restart, the
laboratory provider reconciles its existing receipt rather than calling
`execute` again. Python-level checks and ephemeral memory are not protection
against code running as the trusted writer UID, and this behavior does not
restrict the production `StripeProvider` or its credentials.
