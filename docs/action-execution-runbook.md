# Physical action execution runbook

This is the production gate for assistant-triggered scene activation and safe device writes.
Unit tests and mocked integrations do not satisfy it.

## Preconditions

1. Deploy matching console and agent revisions with both execution flags disabled.
2. Configure a dedicated `AI_ACTION_AUTHORIZATION_SECRET` (at least 32 random characters).
   `AI_SCENE_ACTION_AUTHORIZATION_SECRET` remains a compatibility fallback for scenes.
3. Configure `AI_ACTION_LEDGER_STORE`; the legacy scene store remains a fallback.
4. Select a harmless scene and non-sensitive device and explicitly grant their home settings.

## Verification

- Race one idempotency key across deployed workers. Exactly one claim may own dispatch.
- Interrupt before and after dispatch; a claim without a receipt must stay unknown and never retry.
- Verify completed replay makes no Xiaomi call and a changed request with the same key conflicts.
- Verify stale scene/device revisions, offline devices, unselected devices, unsafe properties, raw
  actions, conditional/quoted/future/negated text, and ambiguous names all fail closed.
- Confirm model messages, logs, responses, and receipts contain no session, DID, MIoT address,
  principal/home ID, raw scene ID, or action secret.

Record the date, revisions, namespace, worker count, sanitized targets, outcomes, and rollback
owner. Enable only the validated flag. Roll back by disabling it; never delete claims or retry an
unknown action.
