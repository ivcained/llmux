# Credential-boundary review

An independent read-only security review on 2026-11-11 found no cross-profile, rotated-key or foreign-listener path that could spend another profile's credit. Resolution fails closed before spend. The review approved release after one portability correction: catalog platforms now target Linux and macOS because child startup uses a Unix process lock.

Confirmed boundaries:

- All marketplace secrets resolve through `agent.secret_scope.get_secret`, never credential files.
- A scrubbed child environment carries only allowed runtime settings and scoped marketplace keys, with the calling profile's `HERMES_HOME`.
- All routes except health require the profile/configuration-bound local bearer, including authorization before POST body parsing. The local bearer is not forwarded upstream.
- Provider URL resolution forces loopback, source keys are not seeded into the provider's credential pool, and hosted resolution uses the target profile's environment.
- Missing secrets cannot mint a predictable keyless capability. Antseed-only operation without any marketplace key is intentionally unsupported.

Review follow-ups completed with failing-then-passing regressions: malformed non-ASCII authorization returns 401 instead of dropping the connection; missing credentials skip child spawning rather than repeatedly starting a process that cannot bind.

Accepted limitations: source key or routing-setting changes require stopping the old proxy safely; a foreign listener causes an actionable resolution exception rather than credential fallback. The production CLI always enables authentication; explicit in-process fixture/embedding construction can omit it and is not a public service entrypoint.

Latest full discovery: 194 tests passed in 51.392 seconds, including the Desktop wrapper (62 scenarios). No paid inference was used. This security review does not establish native installed-Desktop behavior or catalog admission.
