# Physical action execution gates

Scene activation and safe device-property writes are implemented behind independent,
default-off deployment flags. Do not enable either flag until its checklist has been verified
against deployed EdgeOne Blob and recorded in `action-execution-runbook.md`.

## Shared durable executor

- [x] Conditional-create claims keyed by principal, home, and idempotency key.
- [x] Strong claim/outcome reads, conflicts, exact-message grants, and terminal unknown outcomes.
- [ ] Verify concurrent conditional create across deployed EdgeOne workers.
- [ ] Verify crash, timeout, missing-receipt, and replay behavior in the deployed namespace.

## Scene activation

- [x] Revision-bound approval and canonical exact-name action capability.
- [ ] Validate one selected real scene, including replay and stale revision.
- [ ] Enable `AI_SCENE_EXECUTION_ENABLED=true` only after recording validation.

## Safe device properties

- [x] Separate default-off write toggle and a verified read/write property allowlist.
- [x] Current exposure, online state, revision, and value revalidation before claim.
- [ ] Validate one selected real device property, including replay, offline, and stale revision.
- [ ] Enable `AI_DEVICE_EXECUTION_ENABLED=true` only after recording validation.
