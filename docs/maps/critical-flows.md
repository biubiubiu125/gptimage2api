# Critical Flows

Status: current

These sequences identify the lifecycle owner and handoff points for workflows
that cross multiple Modules. They are navigation maps, not substitutes for
contracts or implementation.

## Account import

Manual JSON/credential import writes through the account API and
`AccountService`. CPA and Sub2API use one shared remote-job lifecycle.

```mermaid
sequenceDiagram
    participant UI as Accounts/Settings runtime
    participant API as accountImportsApi/accountsApi
    participant Route as api/accounts.py
    participant Source as CPAImportService or Sub2APIImportService
    participant Job as RemoteAccountImportJob
    participant Accounts as AccountService
    participant DB as Application Database

    UI->>API: Start import with target Account Group
    API->>Route: Validated request
    alt Manual import
        Route->>Accounts: Save one batch
    else CPA or Sub2API
        Route->>Source: start_import(...)
        Source->>Job: reserve and start worker
        Job->>Source: Fetch remote account payloads
        Job->>Accounts: finish with one normalized batch
        UI->>API: Poll import job
    end
    Accounts->>DB: Account Repository transaction
    DB-->>Accounts: Stored batch result
    Accounts-->>Route: Backend action result
    Route-->>API: Stable response projection
    API-->>UI: Result or polled progress
```

`RemoteImportJobCoordinator` enforces the shared active-job rule. Both remote
sources finish through `RemoteAccountImportJob.finish`; source adapters do not
implement a second save lifecycle. The UI owns polling and presentation only.

## Studio Image Task

```mermaid
sequenceDiagram
    participant Studio as Studio image runtime
    participant API as imageTasksApi
    participant Route as api/image_tasks.py
    participant Task as ImageTaskService
    participant Upstream as Image generation services
    participant Assets as ImageStorageService
    participant Store as Image Queue Store

    Studio->>API: Create generation or edit task
    API->>Route: Owner-scoped request
    Route->>Task: Create task
    Task->>Store: Persist queued/running projection
    Task->>Upstream: Execute attempts and account switches
    Upstream->>Assets: Publish successful Image Assets
    Task->>Store: Persist terminal result
    Studio->>API: Poll owner-scoped task list
    API-->>Studio: Final task projection and asset URLs
```

`ImageTaskService` owns task admission, transitions, attempts, resumption, and
terminal state. The queue is persisted in the separate
`gptimage2api_image_queue` PostgreSQL database and runs bounded workers inside
the same App process; there is no Cluster role or separate `image-worker`
container in this deployment. `ImageStorageService` exclusively owns Image
Asset catalogue and storage mutations. Studio owns browser conversation
presentation and polling, not task truth.

## Registration core-result handoff

```mermaid
sequenceDiagram
    participant Console as Registration console
    participant API as api/register.py
    participant Register as RegisterService
    participant Core as OpenAI registration state machine
    participant Pending as register_core_results_pending.json
    participant Verify as Account verification
    participant Accounts as AccountService

    Console->>API: Start registration or retry pending result
    API->>Register: Start or reconcile action
    Register->>Core: Acquire mailbox and complete upstream registration
    Core->>Pending: Persist access/refresh/id token result
    Register->>Core: Finalize mailbox success state
    Register->>Verify: Validate account identity and image quota
    Verify-->>Register: Verified account metadata
    Register->>Accounts: Idempotent account save and refresh
    Accounts-->>Register: Account visible to image worker
    Register->>Pending: Remove only after save and refresh succeed
    Register-->>API: Sanitized status and recovery summary
    API-->>Console: Result without credentials
```

The pending file is a durable handoff buffer rather than a manual endpoint.
`RegisterService` retries eligible records with bounded backoff, and the admin
API exposes a credential-free list plus a forced retry action. A record is only
removed after verification, Account Repository persistence, and account refresh
all succeed; an error keeps the record for the next retry.
Mailbox success is finalized before Account Repository persistence. If mailbox
finalization fails, the account is not persisted and the pending core result is
kept for recovery.

## Backup restore maintenance

```mermaid
sequenceDiagram
    participant Admin as Admin console
    participant API as api/system.py
    participant Backup as BackupService
    participant Register as RegisterService
    participant Queue as ImageTaskService
    participant Store as Application/Queue Stores

    Admin->>API: Confirm restore
    API->>Backup: restore_backup(key)
    Backup->>Register: Stop registration writer
    Backup->>Queue: Stop image queue writer
    Backup->>Store: Replace database and file snapshots
    Backup-->>API: Restored result, requires_restart=true
    API-->>Admin: Restart required
```

`BackupService` serializes restore operations and the App configures maintenance
hooks for the live registration and image queue services. It does not resume
old in-memory workers after replacing their persistence.

## Call Record, live monitor, and metrics

```mermaid
sequenceDiagram
    participant Route as Public API route
    participant Call as LoggedCall
    participant Live as RealtimeMonitorService
    participant Logs as CallRecordService
    participant Metrics as DashboardMetricsService
    participant DB as Application Database

    Route->>Call: Wrap invocation
    opt Instrumented image request
        Call->>Live: start and stage events
    end
    Call->>Call: Resolve final result and diagnostics
    opt Instrumented image request
        Call->>Live: finish Active Request
    end
    Call->>Logs: Add final Call Record
    Logs->>DB: CallRecordRepository
    Metrics->>Logs: Incremental synchronization
    Metrics->>DB: DashboardMetricsRepository
```

The Call Record is durable final truth. Active Requests and live operations are
process-memory observations and may disappear on restart. The Dashboard Metric
Projection reads only Call Records after its stored sequence during normal
synchronization, updates affected hourly rows atomically, and rebuilds only when
an explicit repository operation rotates the Call Record generation or an
aggregate table is structurally incompatible. User-facing log deletion and
automatic retention preserve the cursor, so the 30-day history remains
independent of the shorter Call Record retention window. If only the projection
state table is recreated, startup checkpoints the current cursor without
deleting compatible hourly aggregates.

## Dashboard projection

```mermaid
sequenceDiagram
    participant Page as Dashboard/useDashboardPage
    participant API as statsApi
    participant Route as GET /api/dashboard
    participant View as build_dashboard_view
    participant Metrics as DashboardMetricsService
    participant Accounts as AccountService
    participant Runtime as RuntimeEnvironment/RealtimeMonitor

    Page->>API: Fetch dashboard immediately or on timer
    API->>Route: Authenticated request
    Route->>View: Build one response
    View->>Metrics: Read stored multi-range snapshot
    View->>Accounts: Read Account Pool stats
    View->>Runtime: Read runtime and live-operation snapshots
    View-->>Page: Stable Dashboard projection
```

The backend owns metric definitions, available ranges, units, health, and
runtime values. Account cards and Active Request counts read current state;
24-hour call cards and all chart ranges come from the same 30-day hourly
projection; runtime-environment samples are not persisted. The page selects one
returned range and owns refresh timing, retained snapshots, chart construction,
animation, layout, and responsive behavior.

## Managed-container online update

```mermaid
sequenceDiagram
    participant Shell as AppShell update overlay
    participant API as versionApi
    participant Status as UpdateStatusService
    participant Task as UpdateService
    participant Release as GitHub Release
    participant Runtime as Managed runtime directory

    Shell->>API: Check update status
    API->>Status: Read latest release projection
    Status->>Release: Fetch release metadata/changelog
    Shell->>API: Confirm and start update
    API->>Task: Start one update task
    Task->>Release: Resolve and download validated asset
    Task->>Runtime: Install files and synchronize dependencies
    Task-->>Shell: Persisted progress via polling
    Task->>Runtime: Exit for container restart
```

`UpdateService` owns the complete update task and rejects unsupported runtime
modes. The frontend only confirms, starts, polls, and renders progress. Update
task state is persisted in `data/update_task.json`; the container runtime owns
restarting the process after exit.
