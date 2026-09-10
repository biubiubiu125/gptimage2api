# 0001: gptimage2api integration

Status: implementation-verified-with-runtime-gaps

- Owner: gptimage2api maintainers
- Created: 2026-09-03
- Updated: 2026-09-06
- Related issues/ADRs: [ADR 0002](../adr/0002-studio-uses-asynchronous-image-tasks.md), [ADR 0003](../adr/0003-deepen-image-storage-before-replacing-the-catalog.md), [ADR 0004](../adr/0004-use-one-application-database-with-domain-repositories.md), [ADR 0005](../adr/0005-single-app-persistent-image-queue.md)

## Problem and user outcome

The current local `chatgpt2api` fork combines a custom image queue, image
worker cluster, and registration workflow with the older application
structure. The desired product is a new `gptimage2api` project whose
application, account pool, console, registration machine, selected mailbox
providers, and high-volume image execution are maintained as one deployable
application.

The user-visible outcome is a new sibling project that:

- exposes the upstream OpenAI-compatible API and console;
- runs durable Image Tasks through one App process with bounded in-process
  Image Workers;
- retains the current registration workflow with only `yyds_mail`, `remail`,
  `outlook_token`, and `icloud_api`;
- writes successful registration results into the upstream Account Repository;
- keeps the existing `chatgpt2api` checkout and data available for rollback.

## Current behavior and evidence

- The new repository is based on upstream commit `d0cde866eb5e003aa407cb81ba61a65c456f13cb`.
- The upstream baseline has an Application Database, an asynchronous Image Task
  surface, an account service, and a Vue console.
- The local source contains the registration implementation under
  `api/register.py`, `services/register_service.py`,
  `services/register_config_store.py`, and `services/register/`.
- The local source contains the durable image queue under
  `services/image_queue/`; the new integration runs it inside the App process.
- Registration uses `services/register/types.py` for its runtime window and
  no longer depends on Cluster/image-worker integration.
- The local checkout has pre-existing changes in
  `deploy/install.sh` and `tests/scripts/test-install-script.sh`; these files
  belong to the old project and are not part of this change.
- The target environment has Python 3.12 on Windows and Python 3.13.5 in WSL.
  The project requires Python >=3.13.

## Scope

- In scope: create `gptimage2api` from the upstream baseline;
- In scope: integrate the local durable Image Task queue into the single App
  runtime;
- In scope: integrate the registration backend and console;
- In scope: retain exactly four mailbox providers:
  `yyds_mail`, `remail`, `outlook_token`, and `icloud_api`;
- In scope: adapt registration output to the upstream Account Repository;
- In scope: use one PostgreSQL service with separate Application Database and
  Image Queue persistence boundaries;
- In scope: rename product-facing package, runtime, container, storage, and
  environment identifiers to `gptimage2api`;
- In scope: document migration boundaries for accounts, User Keys, registration
  configuration, mailbox state, and optional image assets;
- In scope: update current maps, requirements, deployment documentation, and
  `CHANGELOG.md` as behavior changes are implemented.

## Non-goals

- Not included: modifying or deleting the existing `chatgpt2api` checkout;
- Not included: pushing, tagging, publishing, or building/pushing release
  images;
- Not included: migrating active legacy Image Task leases or Cluster state;
- Not included: retaining legacy mailbox providers outside the four approved
  providers;
- Not included: restoring a multi-node Cluster or requiring a separate
  `image-worker` container in the initial deployment;
- Not included: replacing the accepted ImageStorageService ownership boundary;
- Not included: putting Image Task state into the Application Database;
- Not included: introducing a second image task lifecycle beside the durable
  queue.

## Domain language

Use the terms in [`../../CONTEXT.md`](../../CONTEXT.md):

- **Upstream Account**: a ChatGPT credential selected for an upstream request;
- **Account Pool**: the eligible set of Upstream Accounts;
- **Image Task**: an owner-scoped asynchronous image generation or edit request;
- **Image Attempt**: one upstream-account attempt within an Image Task;
- **Image Asset**: an image published by the ImageStorageService;
- **Application Database**: the shared SQLite or PostgreSQL database for
  structured control-plane repositories;
- **User Key**: a local bearer credential for API and console capabilities.

Proposed term:

- **Image Queue Store**: the durable persistence adapter for Image Task
  admission, execution leases, retries, and terminal task state. It is separate
  from the Application Database and is owned by the Image Task runtime.

## Unique owners

| Concern | Authoritative owner | Interface consumed by others | Forbidden mirror or fallback |
| --- | --- | --- | --- |
| Business meaning and task projection | `ImageTaskService` and its contracts | `api/image_tasks.py`, `web-vue/src/api/` | Frontend-derived task classification |
| Image Task state transitions | `services/image_queue/` adapter behind `ImageTaskService` | Image API and Studio routes | Upstream bounded runner and local queue both executing images |
| Registration state transitions | `RegisterService` and registration state machine | `api/register.py`, registration Vue runtime | Queue or frontend-owned registration state |
| Account persistence | Upstream Account Repository | AccountService and registration adapter | Direct registration writes to account tables |
| Image persistence | `ImageStorageService` | Image Task runtime and Gallery | Direct catalog/file mutation by queue code |
| Queue concurrency and cleanup | Image Queue runtime | App lifespan and task routes | Registration thread limits controlling images |
| Registration concurrency and cleanup | Registration runtime | App lifespan and register routes | Image Worker pool controlling registration |
| Mailbox provider selection | Registration provider registry | RegisterService and registration UI | Frontend provider allowlists diverging from backend |
| Console business projections | Backend view/projection modules | Vue API adapters | Vue inference from raw flags or error text |

## Backend Modules and Interfaces

The integration changes these boundaries:

- `api/ai.py` and `api/image_tasks.py` keep stable external contracts and submit
  work through one ImageTaskService owner.
- The ImageTaskService adapter must expose task creation, owner-scoped reads,
  cancellation where supported, resumption, and terminal result projection
  while delegating durable state to the Image Queue Store.
- `services/image_queue/` retains task models, repository, scheduler, worker,
  retry policy, recovery, idempotency, resource controls, and artifact
  publishing. Cluster-only role checks are removed from its application
  integration.
- `services/register_service.py` owns registration scheduling, lease, target
  calculations, and operation results. It uses an independent bounded runner.
- `services/register/` owns provider implementations, the OpenAI registration
  state machine, mailbox state transitions, and redaction.
- `services/register_config_store.py` owns registration configuration
  and runtime lease persistence in the Application Database.
- A registration account adapter normalizes successful registration results
  into the upstream AccountService interface; registration code does not write
  account tables directly.

Errors must preserve provider, stage, task, and account diagnostics without
exposing Access Tokens, Refresh Tokens, User Keys, mailbox secrets, or provider
credentials.

## Frontend interaction and responsive behavior

The registration page is added to the upstream AppShell and router. It retains
the current registration controls and uses only the four approved providers.
The page consumes backend projections for provider status, runtime state,
capabilities, and action results. It owns only drafts, selection, loading,
polling, dialogs, and responsive layout.

Image Task pages continue to show queued, running, retrying, terminal, and
error states from the backend projection. Polling stops for terminal tasks and
is cleaned up when the page is destroyed. The page does not reconstruct queue
state from error text or raw database fields.

Desktop and narrow-screen checks must cover the registration page, queue task
list, provider forms, operation results, and modal focus behavior.

## Persistence impact

- Application Database: Upstream Accounts, User Keys, settings, proxy
  configuration, registration configuration, and registration runtime lease.
- Image Queue Store: Image Task state, attempts, execution leases, retry
  schedule, and terminal task references. It remains outside the Application
  Database.
- ImageStorageService: image assets, gallery catalogue, tags, local files, and
  optional WebDAV content.
- File state retained for registration compatibility:
  `outlook_token_used.json` and `register_core_results_pending.json`.
- `data/register.json` is a one-time import source, not a second authoritative
  store after migration.
- Backups must include registration configuration/state and Image Queue Store
  data without exposing secrets.

## Concurrency, security, and failure behavior

- Image admission is bounded globally and per Proxy Reference / Upstream
  Account. Queue workers lease one task at a time and release or retry it
  atomically.
- Registration uses a separate bounded capacity, schedule, and runtime lease.
- A task is idempotent by its request identity where the public contract
  supports one; a worker restart must not leave an unowned task permanently
  running.
- Retryable upstream failures use bounded backoff. Non-retryable failures
  become terminal with sanitized diagnostics.
- Admin-only registration actions require the existing control-panel
  capability. User-owned Image Tasks remain owner-scoped.
- External mailbox, auth, proxy, and asset requests retain protocol,
  destination, response-size, redirect, and timeout validation.
- Codex Responses image generation uses the same configured `Session`, proxy
  profile, image egress reservation, concrete target route, and bounded
  deadline as the other image paths. Transport failures remain
  account-attributable instead of becoming internal errors.
- Registration runtime renews its database lease while mailbox futures are
  waiting, cancels queued futures during shutdown, and rejects Outlook reset
  requests while the runtime is active. Outlook state mutations use a
  cross-process file lock.
- Database-backed registration configuration is AES-GCM encrypted. Legacy
  plaintext rows are read once and migrated; writes fail when no encryption
  key is configured.
- Backup restore enters maintenance mode, stops registration and image queue
  writers, serializes concurrent restore operations, and returns
  `requires_restart: true`.
- Tokens and provider credentials are masked in responses, logs, errors,
  backups, and test output.

## Verification snapshot (2026-09-06)

- WSL backend regression suite: `157 passed`; only the existing FastAPI/Starlette
  deprecation warnings remain.
- WSL Python byte-compilation: passed.
- WSL frontend contract runtime check: passed.
- WSL frontend production build: passed.
- Real Docker Compose/PostgreSQL smoke, live mailbox-provider calls, and
  desktop/narrow-screen browser checks are still pending because this WSL
  environment has no Docker daemon and no configured external provider
  credentials.

## Acceptance criteria

- [ ] A clean checkout starts one `gptimage2api-app` and one PostgreSQL service
      without Cluster or `image-worker` containers.
- [ ] `/v1/models`, `/v1/images/generations`, `/v1/images/edits`, and the
      asynchronous Image Task routes use the durable Image Queue Store.
- [ ] Multiple Image Tasks can queue, execute with bounded concurrency, retry,
      finish, and recover after an App restart.
- [x] The registration console exposes exactly `yyds_mail`, `remail`,
      `outlook_token`, and `icloud_api`.
- [ ] A successful mocked registration is normalized into an Upstream Account,
      refreshed, quota-checked, and visible in the upstream Accounts page.
- [ ] Registration continues independently while Image Workers are busy.
- [ ] Existing accounts, User Keys, registration settings, mailbox state, and
      optional assets can be migrated without changing the old checkout.
- [x] Secrets are absent from logs, API projections, encrypted registration
      configuration rows, and test output.
- [x] Backend targeted tests, Python regression checks, frontend runtime checks,
      and frontend production build have fresh evidence.
- [x] Current maps, deployment docs, references, and `CHANGELOG.md` no longer
      describe removed Cluster/image-worker behavior as current.

## Verification matrix

| Acceptance criterion | Verification level | Evidence required | Status |
| --- | --- | --- | --- |
| Single App + PostgreSQL deployment | Integration / smoke | Compose config and `/health` response | partial: Docker smoke unavailable |
| Durable Image Task lifecycle | Contract / integration | Queue tests and restart recovery test | partial: WSL suite passed, real restart pending |
| Four-provider registration | Contract / unit | Provider registry, fallback, and form tests | verified: source and runtime contracts |
| Registration-to-account handoff | Unit / integration | Mailbox finalization ordering, pending-result reconciliation and AccountService assertion | unit verified; live provider pending |
| Independent registration capacity | Unit / integration | Concurrent queue/register test | partial: separate scheduler verified, concurrency pending |
| Migration boundaries | Script / dry run | Ordered Image Queue migration and isolated fixture migration report | ordered migration verified; live PostgreSQL pending |
| Secret redaction | Unit / contract | Redaction tests, encrypted DB row assertion and output scan | unit paths verified; full production backup scan pending |
| Frontend behavior | Runtime / build / browser | Runtime test, build, desktop and narrow-screen check | partial: runtime/build passed, browser check pending |

## Documentation and CHANGELOG impact

Update after implementation changes are verified:

- `CONTEXT.md` for the Image Queue Store relationship if required;
- `docs/maps/system-map.md`, `backend-map.md`, and `critical-flows.md`;
- `docs/deployment.md` and relevant runbooks;
- `docs/references/upstream-projects.md`;
- `README.md`, `README_EN.md`, and `.env.example`;
- `CHANGELOG.md` under Unreleased.

## ADR requirement

ADR 0005 records the durable decision to keep Image Tasks outside the
Application Database while executing the durable queue inside the single App
process. No change is made to ADR 0003 ImageStorageService ownership or ADR
0004 Application Database ownership.

## Rollback

Keep the old `chatgpt2api` checkout, its existing data directory, its database
backup, and its deployment configuration unchanged. Deploy
`gptimage2api` with separate names, port, data directories, and databases until
API, queue, registration, and migrated-account smoke tests pass. Rollback is
a deployment switch back to the old project; no dual-write behavior is added.

## Unresolved questions

- None for the approved initial scope. External publication, push, release
  tags, and production cutover remain separate user-authorized operations.
