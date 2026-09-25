# Implementation gates

## Phase 3 — Safe scene action

- [x] Add a default-deny, per-home scene approval control to the console exposure settings.
- [x] Return normalized action summaries and a content revision hash for enabled manual scenes without static risk filtering.
- [x] Reject stale revisions and require individual approval or a confirmed home-level approval bypass.
- [x] Add an immutable Blob claim and outcome receipt keyed by principal, home, and idempotency key.
- [ ] Enforce direct present-tense intent in the canonical assistant action capability; the deprecated command router must not perform physical writes.
- [x] Keep the executor feature flag disabled by default and make uncertain outcomes terminal.
- [ ] Verify conditional create plus strongly consistent reads under concurrent deployed EdgeOne workers.
- [ ] Verify crash/timeout replay behavior against the deployed Blob namespace.
- [ ] Complete one selected real-scene end-to-end validation after the preceding checks; record the result and enable execution only through deployment configuration.
- [ ] Register scene actions only through the canonical assistant after the deployment checks above pass and the console issues a trusted per-request action scope.

Do not enable `AI_SCENE_EXECUTION_ENABLED` until every unchecked operational gate above is complete. A missing terminal receipt after a claim is an unknown outcome and must never trigger another scene run.
