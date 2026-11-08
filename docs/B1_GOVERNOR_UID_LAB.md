# Governor to signed UID service: synthetic laboratory only

`ExperimentalGovernorUidProvider` is an opt-in adapter for testing the
historical Governor against the signed positive UID service. It never calls a
real provider and always reports `UNKNOWN` to the Governor. A synthetic
`EXECUTED` receipt records a laboratory effect; it does not prove an external
payment, authorize execution in production, or justify budget release.

The adapter verifies the request against the Governor's current DISPATCHING
record and checks the connected server's Linux UID before transmitting the
request. The service independently verifies the signed approval and operates
under a separate writer UID. The signed envelope must be present as the
Governor's authorized payload at adjudication so I2 checks the same payload
at dispatch. The signer and its test key do not establish the provenance or
correctness of a real policy decision.

The Governor registry/budget and the service's native SQLite budget are
distinct. Their checks and commits are not one transaction. This prototype
does not prove an integrated I8 invariant, recovery after Governor restart, a
production authority handover, or end-to-end enforcement. The provider's
`reconcile()` deliberately remains unresolved and never turns a laboratory
receipt into real-provider confirmation. A worker may still use other code or
provider credentials unless the deployment independently isolates both.

The historical `StripeProvider`, other connector paths and real credentials
are not routed through this adapter. The adapter and service are not a supported
production deployment or packaged public interface. B1, B2, integration and
production remain HOLD until the authority, policy issuer, credential/network
isolation, all supported entry points, recovery and independent evidence are
qualified together for a specific deployment.
