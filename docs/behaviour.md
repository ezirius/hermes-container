# Bash launcher behaviour

`hermes-container.sh` is the active Bash launcher. Keep `lib/` beside it. The Python
reference has its own design in a separate local worktree, outside this Bash
branch. Its code and records are not migrated by this launcher.

## Commands and host checks

Use `./hermes-container.sh setup <workspace>` for setup, `./hermes-container.sh chat <workspace>` for
chat, or `./hermes-container.sh` to select a workspace and chat. Use `./hermes-container.sh import
[workspace] [backup.zip]` to restore; omit the path for a numbered backup list.
`./hermes-container.sh backup [workspace]` creates a verified native backup on demand.
`./hermes-container.sh start [workspace]` starts or reuses the shared gateway/dashboard container
using saved Hermes authentication; see the [service instructions](../README.md#one-container-for-gateway-dashboard-and-chat).
`./hermes-container.sh stop [workspace]` stops gateways and removes that container, preserving host data.
Setup/chat/start/stop/backup/import require a
terminal; EOF cancels. Help works offline. Green means success, red
means error and amber means warning. `NO_COLOR=1` and redirected output disable
colour.
Success, warning and error messages, and help output, end with a blank line.
All numbered menus, including image updates, retry invalid input and accept
`q` or `Q` to quit. Confirmation prompts use `y`/`yes` or `Y`/`YES` to proceed;
other answers cancel. An interrupt always cancels, even with an affirmative answer.

Invalid arguments print an error followed by a blank line and usage on stderr,
then return status 2 before host checks. Workspace arguments use the same name
characters and length as workspace directories, but accept any letter case.
Help is available only as `--help` or `-h`.

Requirements are Bash 3.2+, jq 1.7+, curl, unzip and Podman. Production needs no
host Python. macOS Apple Silicon requires macOS 27+ and Podman client/engine
6.1.1+; macOS Intel requires macOS 15+ and Podman 5.8.4+; Linux Intel/AMD x86-64
requires Podman 6.1.1+. These are project minimums, not claims that every newer
combination is tested. Actual macOS versions are checked; Linux distribution
versions are not tracked. Linux ARM is outside scope.

The engine must be native rootless Linux with cgroups v2, at least 2 CPUs and
6 GiB RAM. Containers receive 1 CPU, 2 GiB RAM and 512 MiB shared memory. Linux
uses the local engine with `--remote=false`. macOS requires an already running,
rootless Podman VM and a matching default connection; the connection is fixed
for subsequent commands. The launcher does not install tools or upgrade,
start, resize or reconfigure the VM.

Storage checks require 3 GiB free for a cached image or 10 GiB before a pull,
including the VM backing volume on macOS. After locking and creating missing
source directories, temporary markers check that the VM sees current contents
in all four mount sources. Markers are removed after the check. This checks visibility,
not container write permissions, and does not repair VM sharing.

## Workspace paths

Workspaces are immediate directories under `/Volumes/Data`, owned by the current
account. Their spelling starts with one capital letter, then lowercase letters,
digits, underscores or hyphens, with at most 32 characters. Lowercasing the name
must give the current lowercase login username. Input accepts any case:
`ezirius`, `Ezirius` and `EZIRIUS` all select `Ezirius`. No username is hardcoded.
The launcher does not create accounts, rename directories or select another
account's workspace.

For account `ezirius`, the mounts are:

| Host | Container |
| --- | --- |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Home` | `/opt/data` |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Backups` | `/opt/data/backups` |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Agent Docs` | `/Volumes/Data/Ezirius/Apps Data/Hermes/Agent Docs` |
| `/Users/ezirius/Documents/Ezirius/Apps Data/Hermes/User Docs` | `/Users/ezirius/Documents/Ezirius/Apps Data/Hermes/User Docs` |

macOS requires the account home `/Users/<lowercase-login>`. Linux uses the actual
account home from the OS database, normally `/home/<login>`, for the second Docs
source. `/Volumes/Data` remains the workspace base on both systems.

Setup/chat/start mount Home, both Docs directories and Backups read/write. Another
container mounting the same Home, Agent Docs, User Docs or Backups directory
blocks execution, including stopped
containers and symlink aliases. Only the fully inspected shared service is exempt
during service operations. Maintenance requires it to be stopped and removed.
Other directories, including parents and children,
are allowed. Other matching workspace labels still block execution. Different mount
directories can still expose Hermes files; do not run a second Hermes session
through them during setup, chat, start, backup or import. All returned
Podman IDs are inspected and two matching inventories are required. Apple
container inventory is also checked when its CLI is installed on Apple Silicon.

Existing source parents are checked and their identities pinned through the
operation. Home, Docs, Backups and their parent directories may be symlinks:
the launcher mounts their resolved host paths and checks that their destinations
stay unchanged.
Container Docs paths retain the names shown above. Path resolution allows at most
64 combined symlink and parent steps, so cyclic links fail instead of hanging.
Broken links, unsafe target permissions and replaced paths are refused.
Neither resolved Docs directory may
equal, contain or sit inside Hermes Home: Docs are working files, while Home is private
and backed up. Private launcher records and locks
still require regular files/directories. New
directories use 0700; records and archives use 0600. Existing safe Home/Docs
permissions are preserved. Missing source directories are created after locking.

Fresh setup without image configuration or usable legacy records offers import,
new setup or cancellation. It requires empty Home apart from `backups` and safe
regular Finder `.DS_Store` files. Existing Docs may remain. Failed scans never count as empty. Foreign
metadata, old Home/Docs layouts and unexplained existing data require inspection.

## The three records kept

1. **Image configuration:** `${XDG_CONFIG_HOME:-~/.config}/hermes/<Workspace>.json`.
   `XDG_CONFIG_HOME`, when set, must be absolute. The record has exactly `tag`,
   `platform` (`arm64` or `amd64`) and immutable `digest` fields. It records the
   desired image, not proof of a successful run.
2. **Temporary lock:** `<runtime>/hermes/<sha256-of-workspace-path>.lock/`.
   Linux uses `XDG_RUNTIME_DIR`, defaulting to `/run/user/<uid>`. macOS uses its
   private user temporary directory from `getconf DARWIN_USER_TEMP_DIR`, unless
   `XDG_RUNTIME_DIR` is set. The runtime directory must be absolute, private and
   owned by the current account. The lock contains a token and diagnostic process
   identity, not a recovery journal.
3. **Backup receipt:** `Backups/hermes-backup-<date>-receipt.json`, beside its ZIP.
   It holds format version, workspace, operation ID, native filename, SHA-256,
   UTC capture time and source image. No separate backup index is needed.

Exclusive `mkdir` acquires the lock. Directory identities and token are checked
before writes. A lock is never stolen based on age or PID. Normal exit removes
only the owned lock. Uncertain child cleanup or replaced paths preserve it and
show diagnostics. Runtime storage is temporary; this is not a permanent record
of an interrupted operation.

Existing `.hermesagent` records are left untouched. There is no new compatibility
cache, persistent transaction journal or optional launcher settings file. Old
settings no longer disable checks. An old active lock still blocks execution.
When image configuration is absent, explicit setup may offer the image from
valid old Bash records: the pending target, otherwise the accepted image. This
requires confirmation and a native backup before running setup. It does not
replay the old operation or certify its outcome. Existing operation-directory
backups remain readable and follow the same retention policy. Legacy backups
outside the current Backups directory require manual inspection/migration; old
receipt formats in the current Backups directory remain readable without rewriting them.

## Persistent service

Start and chat use one container named
`hermes-<version>-<UTC timestamp>-<workspace>-service-<12-character hash>`.
Temporary containers replace `service` with `setup`, `backup` or `import`.
The creation timestamp stays unchanged on reuse. Discovery uses workspace and
service labels and refuses duplicate matches. Podman records its identity and
configuration; the launcher adds no service-state file. The container runs native `gateway run`, with the dashboard enabled under s6, four
persistent mounts and one loopback-only published dashboard port. Account port
offsets are +10000 for Ezirius, +20000 for Nala and +50000 otherwise, from 9119.
The native gateway API is not published. Saved Hermes authentication is retained.

The launcher checks container name, image digest, labels, command, mounts,
auth-preserving dashboard settings and published port before reuse/removal.
Environment settings must have unique keys; malformed exec-session lists are refused.
Reuse also requires local terminal execution, Agent Docs as the terminal working
directory, and Agent Docs as the configured write-safe root.
A running service is reused; an exited matching service is restarted. Chat joins
it with `podman exec` as the mapped user in Agent Docs. Chat's normal exit does
not stop the service. Start requires one unambiguous JSON object from `/api/status`
with enabled authentication and non-empty provider names,
then prints the container name and URL; messaging platform connectivity and actual login are not
proven by that check. Inspect `podman logs <full-container-name>` if startup fails.
Paths, image configuration and container identity are rechecked after release
checks and before reporting readiness. A responding HTTP endpoint alone does not
count as proof that the expected service container is still running.

Stop requires the operation lock and no active exec sessions. It stops and waits
for the dashboard, calls native `gateway stop --all`, verifies stopped records,
then stops and removes the container without removing host data or volumes.
Command failures name the failed step and the preserved private output files;
later shutdown steps do not run. Setup, backup and import require
this stop first. For `chat`/`start`, the update menu explicitly includes stop,
backup and restart in its approval. The launcher prepares the images and obtains
any encryption acknowledgement before stopping. It rechecks the service identity
after prompts/downloads; a different container requires a fresh command.
An active operation lock or exec session still blocks stopping. A failed stop or
backup prevents image selection and restart; recovery is manual.
Other profile gateways stopped by native `gateway stop --all` remain stopped
until explicitly started, as with an ordinary stop.
Mac service startup requires Hermes
v2026.9.14+ for its protection against enabling WAL on fresh cross-VM storage;
maintenance can still use an older configured image. Existing root/profile
`state.db` and `kanban.db`, plus each home's `kanban/boards/<board>/kanban.db`, block startup
if their headers indicate WAL mode. They need offline conversion; the launcher
does not rewrite databases or authentication. The startup refusal is launcher
policy; Hermes itself reports the existing WAL problem. Hermes' alternative of
using a native VM volume would require a different storage layout here.
Custom database locations are not
inspected, and this header check is not a database-integrity check.

## Updates and execution

1. Check workspace paths, image configuration, gateway records and inventory.
2. Normally check stable GitHub releases and resolve paired native Docker Hub
   digests. Offer continue, update or cancel; first setup asks for acceptance.
   Legacy-image adoption instead uses its explicit confirmation described above.
3. Check host readiness, lock, recheck inputs, prepare directories and verify VM
   shares. Ensure the selected immutable image is available.
4. For a `chat`/`start` update, the approved option authorises stopping the service
   and creating a verified native backup. Repeat setup requests backup confirmation
   separately. Use the configured image (or legacy image during adoption). A failed backup
   stops before changing configuration or starting the selected image.
5. Save the selected-image configuration atomically **before** it can change Home,
   then run setup or start the shared gateway/dashboard service. Failure keeps this selection and the backup. No automatic
   rollback or interrupted-operation repair takes place.
6. After successful setup/update, prune verified backups older than 14 days.
   Cleanup failure warns without undoing the session.

Release requests use fixed HTTPS origins, no redirects, bounded sizes/timeouts
and complete sequential pagination. Registry tokens use private temporary curl
configuration. Tag drift and downgrades are refused. An unavailable release
check may use the configured cached image; first setup cannot use that
fallback. Offline fallback never pulls. Malformed responses, unexpected content
types and oversized responses are errors, not offline fallback conditions.
Invalid or inconsistent release metadata prints an error before stopping.
An unavailable first check reports that setup/import needs an online image selection.

Images come from `docker.io/nousresearch/hermes-agent` and run by digest with
`--pull=never`. Maintenance containers use `--rm`; the service instead uses
`-d --restart=unless-stopped`. Setup attaches to the terminal; chat uses exec in
the service. Only the loopback dashboard port is published. No host sockets or
privileged mode are added. The official entrypoint drops to the operator UID/GID;
Linux uses `--userns=keep-id --user=0:0` and shared SELinux labels for Docs.

The Agent Docs path is Hermes' working directory and file-tool write-safe root.
Initialisation starts in `/opt/data` because the image's s6 startup mishandles
spaces in its initial working directory. For setup, a shell enters the quoted
`TERMINAL_CWD` after the privilege drop; chat exec sets its working directory
directly. The gateway receives Agent Docs through `TERMINAL_CWD`. Root/profile
gateway records are checked: maintenance refuses running intent; service startup
allows it. Unknown, malformed or symlinked records are refused. The dashboard is
enabled only in the persistent service. Brave is deferred.

Signals reach the owned child. Cancellation is checked before new captured commands,
interactive startup, backup publication and retention deletion.
Chat checks cancellation again after its final inventory check, before opening exec.
Post-run inventory and recorded container IDs detect unexpected surviving containers;
uncertainty retains lock/diagnostics. Residual containers are not automatically
removed by a separate cleanup command.

## Native backups and manual recovery

An explicit `backup` requires image configuration and uses that cached image. It
does not check for updates, pull images, run setup/chat/start or change image selection.
It uses the same lock, inventory, native capture and verification checks as a
pre-update backup. A failed backup does not trigger retention.

The configured image runs `hermes backup --keep 0 --output` into a new
`/opt/data/backups/.pending-<operation>` directory. Home and the separate Backups
directory are mounted for backup; Docs are not. TMPDIR stays inside staging and
TZ is UTC. Native Hermes excludes its
`backups` subtree; host verification rejects archives containing it. The native
name remains `hermes-backup-YYYY-MM-DD-HHMMSS.zip`; its receipt uses
`hermes-backup-YYYY-MM-DD-HHMMSS-receipt.json`. Existing archive or receipt
filenames are never overwritten; a same-second collision stops publication and preserves staging for inspection.

Verification requires a successful command, one recognised completion line, no
backup warnings, one non-empty ZIP, expected Hermes members, valid ZIP integrity
and a checksum. Only the exact observed s6 essential-service shutdown notice is
ignored. Unsafe paths, duplicate, encrypted or special members and file/directory
collisions are refused. These checks cannot prove that every intended source file
was captured or that a future Hermes release will restore it correctly.
Archive listings may replace control characters with printable text. Actual Home
filenames are therefore checked before backup and after staged import; the ZIP
listing alone is not proof that the original filenames are safe.

A verified archive and receipt are published immediately under
`Backups/`, before setup/update runs. Successful capture logs
and empty staging directories are removed; failed captures and unexpected files
remain for inspection. Partial publication is not automatically repaired.
Backup capacity estimates twice the larger logical/allocated source size plus
1 GiB, excluding existing backups. This is not reserved space. Capture times out
after 30 minutes. Backups can contain credentials; macOS checks FileVault and
Linux asks for acknowledgement without detecting encryption.

Keep **all verified backups from the last 14 × 24 hours**, with no count limit.
Pruning runs after successful setup/update or an explicit backup, using receipt
capture times.
The exact boundary, future timestamps, pending captures and unrecognised files
are preserved. Before removal, receipts (including source release dates), ZIPs,
hashes and identities are rechecked.
Only old verified archive/receipt pairs are removed; extra files are preserved.
There is no daily scheduler.

Recovery is manual. Inspect the selected image, Home, backup receipts and any
residual container before retrying. A failed setup does not create a persistent
marker blocking chat; the operator decides when Home is safe to use again.
Changing an image selection does not restore data. `import` is a separate,
explicit command. Without a path it lists recognised native ZIP filenames in
Backups and old operation directories, newest filename first. Hidden pending
captures are excluded. A list entry is not a claim that its ZIP is verified.
Numbered workspace, setup and backup menus accept `q` or `Q` to quit.
Invalid numbers, text or blank input prompt again; EOF cancels.
No backups produces a clear error.
Explicit ZIP paths may be relative or absolute. Archives must be owned by the
operator, regular files and not group/world writable.

Import copies the archive into private `Backups/.import-<operation>/`,
checks the ZIP and any adjacent receipt, and confirms use without a receipt when
one is absent. A receipt from a newer Hermes version than the selected image is
refused; the version check does not guarantee compatibility. External-provider `_external/` members require manual import:
their native destination is outside the persistent Home mount. Capacity requires
twice the uncompressed ZIP size plus 1 GiB free after copying. After staging and
the safety backup, free space on the Home volume is checked for one live copy plus
1 GiB. The initial staging check uses the Backups volume.
These are estimates, not reserved space.

The selected/recorded image runs native `hermes import /import.zip --force` first
against a temporary Home, with read-only ZIP input, no network and no Docs mounts.
The launcher requires clean output, a non-empty restore, matching root config and
.env bytes when archived, safe gateway state and actual restored filenames without
control characters or special files. Ordinary Unicode names and spaces are allowed.
These checks do not establish full database integrity or provider functionality.
If no config existed before import, the exact observed startup warning about a
pre-version-12 seeded config is allowed only when the restored config matches the
archive byte for byte. Other warnings still stop import; backup checks grant no
such exception. Startup logs remain available in staging on failure.
After explicit confirmation,
existing Home data receives a verified native backup before the live import.
The live import mounts the separate Backups directory at `/opt/data/backups`;
the staged import keeps that path inside its temporary Home.
The live import uses the same image and ZIP. Native import overwrites matching
files; files absent from the ZIP remain. This is not a directory replacement.

An existing image configuration is retained. An empty, unconfigured Home selects
an image and saves that config before live import; import does not combine a
configured workspace's recovery with an update. First import allows existing
backups but refuses unexplained unmanaged Home data. A failed or interrupted
import keeps staging/logs and any pre-import backup; recovery remains manual.
Success removes only the private staging copy, leaves the selected source ZIP,
and does not prune backups. First setup's import option uses this same flow.
Configured or legacy-image setup retains its normal configuration flow.

Backups do not include either Docs directory or launcher configuration. Their
volume may differ from Home's; keep an independent copy to survive backup-volume loss.

## Verification limits

JSON input is checked for duplicate keys, unsafe numeric values, excessive size
and excessive nesting. Launcher records also reject unknown fields. External API
responses are checked for required fields and types; extra fields may be ignored.
Atomic writes and identity checks assume
cooperating writers; shell code does not provide descriptor-based protection
against every filesystem race or prove power-loss durability.

Run `PYTHONDONTWRITEBYTECODE=1 python3 tests/run.py`. Local tests exercise real
temporary files with simulated external services. Complete interactive setup,
chat and recovery with this minimum-state design remain unverified on the target
platforms. Earlier image probes do not establish acceptance of this new lifecycle.

Native backup naming and import behaviour were checked against the
[official v2026.9.11 source](https://github.com/NousResearch/hermes-agent/blob/v2026.9.11/hermes_cli/backup.py).
That historical check does not prove another selected image's behaviour.
