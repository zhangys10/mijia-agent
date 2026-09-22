# Implementation backlog — historical

> **This checklist is historical and is no longer maintained.** It was written against
> an earlier milestone plan and no longer reflects current behaviour: several items
> marked pending here are implemented, and some claims (notably `AI_SCENE_APPROVED_IDS`)
> do not describe the shipped code.
>
> Current status lives in the code-truth comparison at
> [steward-report-alignment.md](steward-report-alignment.md), whose every claim is cited
> to a source file and commit. Deployment is settled: the system runs as two EdgeOne
> Makers projects (webapp; agent + Python).
>
> The content below is retained only for provenance of the extraction work. Do not use
> it to judge what is done.

<details>
<summary>Original milestone checklist (historical)</summary>

## M1 — Repository and read-only integration

Read-only integration of the extracted Python agent with the web console: repository
creation, CI, the companion console patch, the Makers adapter deployment, and the
create/chat/list/delete exercise against a real browser client.

## M2 — Safe execution parity

Durable execution receipts with atomic claim, scene revalidation, and the wiring of
`runManualScene` through that executor instead of the disabled response.

## M3 — State, quota and cutover

Adapter-owned quota reserve/commit/release, the internal quota summary route, the real
KV binding, cutover to the remote backend, and removal of the console's legacy
implementation.

## M4 — Continue original roadmap

Reminder and preference memory, scheduler verification, the Home Assistant executor
adapter, and Siri/Automation Token migration.

</details>
