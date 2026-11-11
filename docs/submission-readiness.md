# LLMUX 0.1.1 submission readiness

## Verified release gates

The owner authorized the catalog PR and hackathon submission on 2026-11-11. Catalog admission and public demo/form publication remain separate from these local release checks.

- Full Python discovery: 194 tests passed in 51.392 seconds, including the Node-wrapper test. Direct Desktop run: 62/62 scenarios passed. These are overlapping, not additive totals.
- `hermes plugins validate . --install-deps --json`: passed, zero warnings, security scan safe, public SDK surface accepted, host ready.
- `hermes plugins doctor . --ci`: passed, provider `llmux`, version 0.1.1.
- Independent credential-boundary review approved after the Linux/macOS platform restriction. Non-ASCII bearers reject with 401; missing keys cannot spawn doomed children. See [credential review](credential-security-review.md).
- Real-component browser QA: 11 states at 1280x800 and 390x844, source/copy hash match, no clipped Switch buttons, no truncated speed placeholders, active trust-tab contrast 5.19:1. Unsupported-direct-provider coverage is described without guessing aliases. Scoped lintapple exits 0 with eight reasoned exceptions for existing SDK/harness controls, intentional name truncation, dense segmented/sort controls and scroll-mask measurement limits. This is not a whole-application accessibility audit.

Component SHA-256: `39b67c2189a7cdb78e04469dcfa2c66e0b0139a97d48e28084702d0bfa052559`.

No paid inference, active-profile installation, gateway restart or native chat mutation was performed. Test upstreams are local synthetic services. Browser QA uses read-only live market payloads and simulated host doors; a captured route plan is not a served reply.

## Release and publication tracking

1. Commit and push this tested tree, then pin its full immutable SHA in the catalog entry.
2. Open the authorized catalog PR against NousResearch/hermes-agent. A PR is not catalog admission or maintainer approval.
3. Publish the final labelled demo on X, strictly under 90 seconds, tagging @NousResearch.
4. Submit both public links to https://form.typeform.com/to/FwJLvOln and verify the receipt.

Publication URLs are tracked in the separate submission package rather than guessed in this release snapshot. Official announcement: https://x.com/NousResearch/status/2118094256438936062. Deadline: 2026-11-11 22:00 UTC / 2026-11-12 03:30 IST.

## Native and paid execution not demonstrated

- Installed-Desktop mid-chat switching and actual applied/deferred timing.
- A real reply routed through LLMUX and its actual upstream receipt.
- A usage ledger row from a real reply. Synthetic-upstream tests cover accounting; direct-provider chats bypass the proxy.

Do not change the owner's active conversation to obtain proof. Native checks require an isolated test chat; paid tests require explicit bounded spend approval.

## Demo truth rules

- Label isolated-host footage as such. Show only the real prices and measurements displayed during capture. Solid prices are a dated manual table; speed and trust are unpublished.
- Price discovery does not imply route support. Unsupported direct chats continue with their existing provider. Seller lists are bounded shortlists; flagged counts can cover the wider market.
- The captured untrusted-seller decision is cancelled, not applied. No paid-reply savings are demonstrated. Estimated duration assumes 1000 output tokens; p50 is a median.
- No hard spend ceiling or custody. Full inference context goes to the selected upstream. The detached loopback proxy can outlive Hermes. Failover and usage recording are off by default.
