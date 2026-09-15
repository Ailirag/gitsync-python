# Native repository session admission

`--jobs 2` still runs two independent export workers. Native platform 8.3.27.2130
has rejected concurrent `ConfigurationRepositoryDumpCfg` for the same storage user
with «Пользователь уже аутентифицирован в хранилище» (native07, two failures).
Separate scratch IBs do not isolate that repository login.

## Phase boundary

- **Serialized per canonical storage + case-folded user:** the complete Designer
  `ConfigurationRepositoryReport` and `ConfigurationRepositoryDumpCfg` process,
  including login, download and process exit. History parsing is outside the lock.
- **Parallel:** creation of private IBs, generic extension bootstrap, private
  `LoadCfg`, `DumpConfigToFiles`, staging/export processing in independent workers.
- The Git writer remains ordered and protected by its existing target/transaction
  locks. These locks and recovery behavior have not been modified.
- No new retry, authentication exception suppression, extra storage users or
  repository mutations. Admission wait is bounded by Designer timeout. Cancellation
  is rechecked after admission before starting a repository download; an already
  waiting worker may wait until the bounded lock timeout.

## Process scope

An OS descriptor lock uses `~/.gitsync/repository-sessions/<SHA256>.lock`.
This namespace is stable across CLI processes, instances, temp roots and workdirs
under the **same OS account on the same host**. It does not use `TEMP` (native
harnesses and real jobs may each override it). Locks are released by the kernel on
process exit; lock files persist and are not unlinked. The key contains neither
password, version nor extension; diagnostic file content is only pid/host.

Local storage paths use `Path.resolve()` and OS case normalization; server URIs
normalize scheme/host and conservatively case-fold repository path. User case
folding may conservatively serialize distinct case-sensitive usernames. DNS
aliases, mapped-drive versus UNC aliases, cross-account/cross-host processes and
external Designer clients are **not a distributed coordination guarantee**:
operators must use one canonical address and coordinate those consumers externally.
Do not share a login across uncontrolled clients and expect this local lock to
protect it. No ACL/server changes are made. Source storage is never a lock location.

Tests include the native07 conflict at the real backend/Designer seam, independent
processing rendezvous (would fail if the whole export were serialized), separate
backend instances with path aliases, another Python process holding the session,
bounded admission failure, and invalid-auth propagation with no retry.
