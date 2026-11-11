# Phase 1 climate profile validation

1. Configure one sensor and one target-temperature/mode actuator in one room; browser
   responses must contain only opaque references.
2. Verify immediate and two-minute visible polling, abort on navigation/home change,
   visibility/network resume, 5/15-minute backoff, and visible suspension state.
3. Verify one sample per room/15-minute bucket and 90-day pruning. Two matching changed
   samples create one external override; a confirmed Console write and matching readback
   create a Console override. Unknown write outcomes create neither.
   For boilers, verify only thermostat/room-heating target and power properties are
   accepted; no domestic-hot-water or separate boiler-controller fallback is allowed.
   A capability-policy revision must remove the affected pair's old samples,
   observations, derived cells and review state before new thermostat sampling.
4. Six observations across three dates learn only the matching time/temperature cell.
   Check conflicts, exclusions, sparse evidence, and identifier rejection.
5. Exercise exclude/restore, derive, review, export and deletion on iPad and keyboard.
6. Prove climate endpoints never call property-set, action, scene, automation, or executor.
7. Run Agent pytest/ruff and adapter tests; run Console typecheck, lint, and tests. Never
   label mocked checks as live EdgeOne, Gateway, KV, or Xiaomi validation.
