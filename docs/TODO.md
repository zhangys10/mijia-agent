# Implementation gates

## Phase 3 — Safe scene action

- [x] Add a default-deny, per-home scene approval control to the console exposure settings.
- [x] Return normalized action summaries, a content revision hash, and a conservative risk classification.
- [x] Reject stale revisions and scenes that cannot be matched to a single supported light/switch target.
- [x] Add an immutable Blob claim and outcome receipt keyed by principal, home, and idempotency key.
- [x] Require a direct present-tense scene command in the Python command ingress.
- [x] Keep the executor feature flag disabled by default and make uncertain outcomes terminal.
- [ ] Verify conditional create plus strongly consistent reads under concurrent deployed EdgeOne workers.
- [ ] Verify crash/timeout replay behavior against the deployed Blob namespace.
- [ ] Complete one low-risk real-scene end-to-end validation after the preceding checks; record the result and enable execution only through deployment configuration.

Do not enable `AI_SCENE_EXECUTION_ENABLED` until every unchecked operational gate above is complete. A missing terminal receipt after a claim is an unknown outcome and must never trigger another scene run.
