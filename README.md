# Hermes launcher

Run the Hermes gateway and dashboard in one persistent Podman container. Chat
opens inside that same container. Home, Docs and backups stay on the host.
Setup, chat and start check releases automatically; updates require confirmation.

## Commands

```sh
./hermes-container.sh                     # Select a workspace and start chat
./hermes-container.sh chat ezirius         # Start/reuse Hermes and open chat inside it
./hermes-container.sh start ezirius        # Start/reuse Hermes and show the dashboard URL
./hermes-container.sh stop ezirius         # Stop Hermes; preserve all host data
./hermes-container.sh setup EZIRIUS        # Configure Hermes or repeat setup
./hermes-container.sh backup               # Select a workspace and create a backup
./hermes-container.sh backup ezirius       # Back up Ezirius now
./hermes-container.sh import               # Select workspace, then choose a backup
./hermes-container.sh import ezirius       # Choose a backup for Ezirius
./hermes-container.sh --help

# Import a specific ZIP; quote paths containing spaces.
./hermes-container.sh import ezirius "/path/to/backup.zip"
```

Workspace input accepts any case. `Ezirius`, `ezirius` and `EZIRIUS` select the
existing `Ezirius` directory. Its name must match your lowercase login username
when lowercased. Directories retain their existing required spelling: one capital
letter, then lowercase letters, digits, underscores or hyphens, up to 32 characters.
The launcher does not rename directories or create accounts.

At numbered menus, enter `q` or `Q` to quit. Invalid numbers or text prompt again;
closing input also cancels.
The update menu uses `1` to continue with the current image and `2` to update.

Invalid command arguments show an error, a blank line and usage, then exit with
status 2. Help is available through `--help` or `-h`.

Launcher operations require a terminal. Green means success, red means
error and amber means warning. `NO_COLOR=1` disables colour; redirected messages
are plain text.
Messages and help output end with a blank line for readability.

## One container for gateway, dashboard and chat

`start` starts or reuses the shared service and prints its full container name
and dashboard URL. `chat` opens terminal chat in that same container, starting
it if needed. All four container types use the same naming pattern:

```text
hermes-<version>-<UTC timestamp>-<workspace>-<purpose>-<12-character hash>
```

| Purpose | Lifetime |
| --- | --- |
| `service` | Persistent; gateway, dashboard and chat share it |
| `setup` | Temporary configuration session |
| `backup` | Temporary native backup |
| `import` | Temporary staged or live restore |

The timestamp is the creation time and stays unchanged when reusing the container.
The launcher finds it by workspace and service labels, then checks its full name.
The old `dashboard` command is removed. The official image runs `gateway run`
with `HERMES_DASHBOARD=1`;
s6 supervises the gateway and dashboard. `chat` uses `podman exec` to open an
interactive session inside this container. Exiting chat leaves both services running.

`start` prints the full container name and URL and returns to your shell.
Use authentication already saved in Hermes Home (`.env` or `config.yaml`); no credentials are requested,
generated or overwritten. The launcher checks that the dashboard responds with
an authentication provider before reporting its URL. This does not verify your
login or messaging integrations. Missing authentication needs configuring through
the [official instructions](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard#authentication).

| Account (case-insensitive) | Host URL | Offset from 9119 |
| --- | --- | --- |
| Ezirius | `http://127.0.0.1:19119` | +10000 |
| Nala | `http://127.0.0.1:29119` | +20000 |
| Anyone else | `http://127.0.0.1:59119` | +50000 |

Only host loopback is published. Hermes listens on container port 9119; its saved
OAuth callback may need to match the host URL. The gateway API port is not
published: the dashboard and gateway share the container. Closing your terminal
does not stop the service. Podman's `unless-stopped` policy handles container
restarts while the engine runs; it does not start the Mac's Podman VM after reboot.
See the [official shared-container design](https://hermes-agent.nousresearch.com/docs/user-guide/docker#running-the-dashboard).

Run `./hermes-container.sh stop <workspace>` before setup, backup or import.
Close chat first. Stop disables the dashboard, waits for it to stop,
uses native `hermes gateway stop --all`, checks stopped gateway records, then
stops and removes the container. Home, Docs and Backups are retained. The next
chat/start command creates it again. Other profile gateways remain stopped
until explicitly started.
If a stop or restart command fails, the error names the step and its preserved
output files. Later shutdown steps do not run after a failure.

When `chat` or `start` offers an update, the menu explains that approving it
also authorises stopping Hermes, taking a verified backup and restarting with
the new image. Continuing with the current image or quitting leaves it running.
An active chat/exec session blocks the stop. Image availability and any required
encryption acknowledgement are checked before stopping. If stopping or backup
fails, the update does not proceed; recovery remains manual.

An unrelated container using the same mount still blocks execution. Reuse is
limited to the expected name, image, labels, mounts and local dashboard mapping.
The launcher rechecks paths, image configuration and container identity after
release checks and before reporting dashboard readiness.
The operation lock protects startup, maintenance and interactive chat; an already
running dashboard can be opened while chat holds that lock.

### Existing SQLite databases on macOS

Mac service startup requires Hermes v2026.9.14 or newer for its cross-VM database
protection. Backup/import can still use an older configured image.

An existing session or Kanban database in WAL mode must be converted **offline**
before gateway, dashboard and chat share it across the Mac/VM mount. The launcher checks
root/profile `state.db`, root/profile `kanban.db` and named boards under each
home's `kanban/boards/<board>/kanban.db`. It refuses to
start that combination; it never silently checkpoints or changes your database.
This refusal is launcher policy. Hermes reports an existing WAL database on a
VM-shared mount and leaves conversion to the operator.
Stop every process using the database, preserve a backup, apply SQLite's
`PRAGMA journal_mode=DELETE`, and set `database.journal_mode: delete` in Hermes
configuration. Repeat for affected profiles. Follow the
[official filesystem guidance](https://hermes-agent.nousresearch.com/docs/user-guide/docker#filesystem-requirements-for-statedb-in-containers).
Hermes also supports keeping WAL on a native volume inside the Linux VM. That
alternative requires changing this launcher's host-folder storage layout.
Custom database locations and other applications' databases are outside this check.

## Data paths

For account `ezirius`:

| Host directory | Container directory |
| --- | --- |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Home` | `/opt/data` |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Backups` | `/opt/data/backups` |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Agent Docs` | Same absolute path; Hermes working directory |
| `/Users/ezirius/Documents/Ezirius/Apps Data/Hermes/User Docs` | Same absolute path |

Agent Docs is the default working directory; User Docs is the account-home mount.
Existing directories named `Docs` are not moved automatically.

Home and Docs may be symlinks. Their real host locations are mounted; container
paths remain as shown above. Backups has its own mount and may also be a symlink.

Both Docs directories are writable.
Neither Docs directory may overlap Hermes Home, including through a symlink.
Backups must remain separate from Home and both Docs directories.
Any container other than the verified shared service mounting the same Home,
Agent Docs, User Docs or Backups directory blocks execution, including through a symlink. Other directories, including parents and
children, are allowed. These mounts can still expose Hermes files, so avoid running
another Hermes session through them.
Other containers labelled for the same Hermes workspace also block execution.
Initialisation starts in
`/opt/data`, then a quoted command enters Agent Docs after the official privilege drop;
this avoids the image's s6 issue with spaces in the initial working directory.

Linux uses the real account home, normally `/home/ezirius`, for the second Docs
source. `/Volumes/Data` remains the workspace base on both operating systems.

First setup offers **import a backup** or **set up new Hermes**; enter `q` or `Q` to quit.
Home must be empty/missing apart from an old `backups` directory and safe Finder
`.DS_Store` files. New backups use the separate `Backups` host directory.
Existing Docs may remain. An existing selected-image config allows repeat setup.
Preserved old Bash records can supply an image after confirmation; foreign layouts
and unexplained data require inspection.

## Backups and updates

`./hermes-container.sh backup [workspace]` creates a backup on demand using the configured,
cached image. It requires an existing image configuration and does not check for
updates or pull an image.

Before repeating setup or applying an update to a configured workspace, the
launcher creates a native Hermes backup using the configured image. The update
menu authorises this backup for `chat`/`start`; repeat setup asks separately.
It checks the captured result, archive integrity and checksum before proceeding.

Backups are stored under:

```text
Host:      /Volumes/Data/Ezirius/Apps Data/Hermes/Backups/
Container: /opt/data/backups/
Archive:   hermes-backup-YYYY-MM-DD-HHMMSS.zip
Receipt:   hermes-backup-YYYY-MM-DD-HHMMSS-receipt.json
```

Keep **all verified backups from the last 14 days**, with no file-count limit.
Pruning runs after successful setup/update or an explicit backup. It removes only verified published
backups older than 14 × 24 hours, using their recorded UTC capture time. The exact
cutoff, future dates, pending captures and unrecognised files are preserved.
There is no scheduled backup or daily cleanup task.

Backups contain Hermes data, potentially including credentials, but not either
Docs directory or launcher metadata. Keep an independent copy to survive loss of
the backup volume. Use `import` to restore explicitly; recovery after an
interrupted import remains manual.

## Import a backup

With no ZIP path, `import` lists backups from the workspace's `Backups/`,
newest filename first, including older operation directories. Select a number or
enter `q` or `Q` to quit. An explicit path can be absolute or relative; quote paths with spaces.
You can also use `./hermes-container.sh import /path/to/backup.zip` and select the workspace.

The launcher verifies a private copy and its receipt when present, then runs
native Hermes import in temporary Home. After checking the result, it asks before
writing live Home and creates a native backup if Home contains data. Matching
files are overwritten; files absent from the archive remain. Import uses the
configured image without an update; an unconfigured empty Home asks for an image.
The restored filenames are checked before live import, including characters that
ZIP listings may hide. Ordinary Unicode names and spaces remain supported.
Backups alone do not prevent importing into an otherwise empty Home.
A receipt naming a newer Hermes version than the selected image blocks import.

Temporary imports have no network or Docs mounts. Failures preserve
`Backups/.import-<operation>/` for manual inspection. Successful imports
remove this staging copy. The selected archive remains. Archives containing
external provider files require manual import because those files would otherwise
land outside the persistent Home mount. First setup's import option uses this
same flow; repeat setup keeps its normal configuration flow.

## Minimum launcher records

| Record | Location and purpose |
| --- | --- |
| Selected image | `~/.config/hermes/Ezirius.json`: tag, native architecture and immutable digest |
| Temporary lock | Linux: `$XDG_RUNTIME_DIR/hermes/<workspace-hash>.lock/`, normally under `/run/user/<uid>`; macOS: the private user temporary directory, under `hermes/` |
| Backup receipt | `Backups/hermes-backup-<date>-receipt.json`: archive identity, checksum, capture time and source image |

An absolute `XDG_CONFIG_HOME` overrides `~/.config`. An absolute, private
`XDG_RUNTIME_DIR` overrides the runtime location. The lock contains a private
token and diagnostic process identity; it is normally removed on exit.

The image config records your **selected image, not successful setup**. It is
saved after any required backup is verified, before starting Hermes. If Hermes
fails, that selection and the backup remain. Recovery is manual: the launcher
neither resumes an interrupted transaction nor restores or downgrades automatically.
A failed setup does not create a durable marker that blocks later chat.

Successful backups retain only the archive and receipt when capture cleanup
succeeds. Failed captures and unexpected files remain for inspection. There is
no compatibility cache, automatic repair journal or launcher settings file.
Existing `.hermesagent` records are preserved; new operations do not write there.
An old active lock still blocks execution and requires inspection.

## Requirements and checks

| Host | Minimum Podman client and engine |
| --- | --- |
| macOS 27+, Apple Silicon | 6.1.1 |
| macOS 15+, Intel | 5.8.4 |
| Linux, Intel/AMD x86-64 | 6.1.1 |

These are project minimums. Use Bash 3.2+, jq 1.7+, curl and unzip. No host Python
is needed in production. Keep `lib/` beside `hermes-container.sh`; there is no installer.
The engine must be native/rootless Linux with cgroups v2, 2 CPUs and 6 GiB RAM.
macOS needs an already running VM sharing the source paths, including
`/Volumes/Data`. Linux needs a local engine and a private runtime directory, normally `/run/user/<uid>`.

Images come from `docker.io/nousresearch/hermes-agent` and run by immutable digest.
The service persists after chat exits; maintenance containers are removed on exit.
Brave integration is deferred. The Python reference has its own design in a
separate local worktree; it is not included in this Bash branch.

Run local tests with `PYTHONDONTWRITEBYTECODE=1 python3 tests/run.py`.
Tests use temporary files and fake external services. The new minimum-state
lifecycle has not yet been validated through complete interactive setup/chat
with real user data on the supported platforms.

See [detailed behaviour and limits](docs/behaviour.md) for the full contract.

## Reading the code

Start with `main()` in `hermes-container.sh`, then follow the named steps.

| Files | Purpose |
| --- | --- |
| `hermes-container.sh` | Arguments, setup and command dispatch |
| `lib/workspace.sh` | Workspace paths, image configuration and operation locks |
| `lib/service.sh` | Persistent service, dashboard readiness and chat |
| `lib/runtime.sh` | Podman checks, mounts and container commands |
| `lib/backups.sh`, `lib/imports.sh` | Native backups, retention and staged import |
| `lib/releases.sh` | Release checks and confirmed image selection |
| `lib/platform.sh`, `lib/safety.sh` | OS differences, files, prompts and child processes |
| `lib/*.jq` | JSON parsing and record validation |
| `tests/run.py` | Local regression tests; no production Python dependency |
