# Hermes Container

Run the Hermes gateway and dashboard in one Podman container. Chat opens inside
that container. Your Hermes data, documents and backups stay on the host.

## Start here

Keep `hermes-container.sh`, `config/` and `lib/` together. Run Hermes Container as your
ordinary account, from a terminal.

```sh
./hermes-container.sh --check-config     # Check Hermes Container settings without starting anything
./hermes-container.sh setup              # Select your workspace and configure Hermes
./hermes-container.sh                    # Start or reuse Hermes, then open chat
```

Your workspace must already exist under the configured workspace base. Its name
must match your login, with the first letter capitalized: account `ezirius` uses
`Ezirius`. Command arguments accept any case. Hermes Container does not create
accounts, rename workspaces or select another account's workspace.

A root-owned workspace base such as `/Volumes/Data` may be group writable when
`WORKSPACE_BASE_ALLOW_GROUP_WRITE=true` (the shipped setting). Set it to `false`
to refuse group write on the base too. The current user needs list and search
access, which can be granted through a named ACL. World-writable bases are refused. User-owned
bases and individual workspaces must belong to the current user and must not
be group/world writable. The launcher does not modify permissions or ACLs.

First setup offers a new setup or a restore. Hermes Home must be empty, apart
from Finder's `.DS_Store`. Existing documents and separate backups may remain.
An existing `Home/backups` directory needs manual inspection or migration first.
At numbered menus, enter a number or `q` to quit.

## Commands

| Command | What it does |
| --- | --- |
| `./hermes-container.sh` | Select your workspace, start Hermes and open chat |
| `./hermes-container.sh start ezirius` | Start or reuse Hermes; print its dashboard URL |
| `./hermes-container.sh chat ezirius` | Open chat in the shared container |
| `./hermes-container.sh stop ezirius` | Stop and remove the container; keep host data |
| `./hermes-container.sh setup ezirius` | Configure Hermes or repeat setup |
| `./hermes-container.sh backup ezirius` | Create and verify a native Hermes backup |
| `./hermes-container.sh restore ezirius` | Choose and restore a saved backup |
| `./hermes-container.sh --check-config` | Validate Hermes Container settings without running Hermes |
| `./hermes-container.sh --help` | Show command syntax |

Omit the workspace to select it from a menu. To restore a particular archive:

```sh
./hermes-container.sh restore ezirius "/path/to/backup.zip"
```

Closing chat leaves the gateway and dashboard running. Close chat and run `stop`
before setup, backup or restore. Stop preserves Home, documents and backups.

## Configuration

Edit **[config/hermes-container.conf](config/hermes-container.conf)** for Hermes Container settings. It
contains paths, image sources, ports, container resources, host requirements,
backup retention, timeouts and input limits. The advanced sections also hold
service endpoints, release page size and process polling intervals. Defaults are
defined there once. Scripts read the settings and perform the work.

Use `NAME=value`, with no quotes around values. Spaces in paths are literal.
Put comments on separate lines. The file is read as data: shell commands,
variables and `~` are not expanded. Only `{workspace}` in the four relative data
paths is replaced with the workspace name. All listed settings are required;
unknown names, duplicates and invalid values identify the setting and its file
location. Missing settings and conflicting timeouts name the keys to fix. Binary
files are refused.

After editing, run `./hermes-container.sh --check-config`. It checks syntax,
values and timeout relationships. It does not check your installed tools, VM,
network or data. Settings are read once per command; edits apply to the next run.
Environment variables do not replace these settings.

`TRUSTED_TOOL_USERS=ezirius` allows tools from the shared Homebrew installation
owned by `ezirius`. Root and the current user are also trusted. Additional account
names can be separated by spaces. Tools must be executable and must not be
group/world writable; leave the list empty to trust only root and the current
user. This setting does not change workspace or data ownership checks.

**Stop Hermes before changing paths, image sources, ports, resources or restart
policy.** These changes do not move data or modify an existing container. Reuse
checks the actual container against the configured settings. If you changed a
setting while it was running, restore the previous value, stop, then edit again.
Changing image sources is only suitable for images compatible with Hermes Container's
Hermes commands, mounts and release format.

There are three separate kinds of configuration:

| File | Purpose |
| --- | --- |
| `config/hermes-container.conf` | Hermes Container settings; edit this file |
| `~/.config/hermes/<Workspace>.json` | Selected image tag, architecture and digest; managed by Hermes Container |
| Hermes Home's `config.yaml` and `.env` | Hermes preferences and credentials; managed through Hermes setup |

An absolute `XDG_CONFIG_HOME` changes the selected-image configuration base.
`XDG_RUNTIME_DIR` can select a private runtime directory. `NO_COLOR=1` disables
terminal colors. Authentication is never generated or replaced by Hermes Container.

## Default storage and dashboard

For account `ezirius`, the default paths are:

| Host directory | Container directory |
| --- | --- |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Home` | `/opt/data` |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Backups` | `/opt/data/backups` |
| `/Volumes/Data/Ezirius/Apps Data/Hermes/Agent Docs` | Same absolute path; default working directory |
| `/Users/ezirius/Documents/Ezirius/Apps Data/Hermes/User Docs` | Same absolute path |

Linux uses the account's actual home for User Docs. Safe source symlinks are
supported. Home, Backups and Docs must remain separate; resolved paths and
ownership are checked. The container's `/opt/data` layout is part of the Hermes
image contract. Host locations are configurable.

The dashboard is published only on host loopback. Default host ports are `19119`
for Ezirius, `29119` for Nala and `59119` for other accounts. Edit
`DASHBOARD_PORTS` to change them. Start prints the URL after checking that the
dashboard reports authentication. Use your saved Hermes login; see the
[Hermes dashboard instructions](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard#authentication).
This readiness check does not prove login or messaging integrations work.

## Updates, backups and restore

Setup, start and chat check for releases. An update requires confirmation. For a
running service, the update choice includes stopping Hermes, creating a verified
backup and restarting. A failed backup prevents the update. When release services
are unavailable, an already configured, cached image can be used without pulling.

An explicit `backup` uses the configured cached image. It does not update or pull.
Backups contain Hermes Home, potentially including credentials. They do **not**
include either Docs directory or Hermes Container configuration. Keep an independent copy
of these files and your backups.

Each verified ZIP has a checksum receipt beside it. The default retention is all
verified backups from the last 14 days, with no count limit. Cleanup runs after a
successful backup, setup or update; there is no daily scheduler. Unknown files,
failed captures and unverified archives are preserved.

Restore checks space before making a private ZIP copy. Copying supports
cancellation and has a timeout. It verifies the copy in a temporary Home, then
asks before writing live Home and backs up existing data. Matching files are overwritten;
files absent from the archive remain. Restore does not update a configured image.
Invalid or oversized size reports stop the operation; they cannot wrap around
and pass a disk-space check. Failures preserve staging and logs for manual recovery. See
[the recovery details](docs/behaviour.md#native-backups-and-manual-recovery).

## Requirements and troubleshooting

Default minimums in the configuration file:

| Host | Podman client and engine |
| --- | --- |
| macOS 27+, Apple Silicon | 6.1.1+ |
| macOS 15+, Intel | 5.8.3+ |
| Linux, Intel/AMD x86-64 | 6.1.1+ |

These are project compatibility minimums, not claims of security or full platform
validation.
Bash 3.2+, jq 1.7+, curl and unzip are required; production does not use host Python.
Tools must belong to root, your account or a configured `TRUSTED_TOOL_USERS`
account, and must not be group/world writable.
The default engine requirement is native, rootless Linux with cgroups v2,
2 CPUs and 6 GiB RAM. macOS needs an already running Podman VM sharing the data
paths. Hermes Container does not install tools or configure or start that VM.

If startup fails, inspect `podman logs <container-name>`. If a lock or failed
operation is retained, inspect the paths named in the error before retrying.
Do not delete recovery files simply to bypass a refusal.

On macOS, existing SQLite databases using WAL need offline conversion before
shared gateway/dashboard/chat use. Hermes Container refuses them; it does not rewrite
databases. Stop every writer, preserve a backup and follow the
[Hermes filesystem guidance](https://hermes-agent.nousresearch.com/docs/user-guide/docker#filesystem-requirements-for-statedb-in-containers).
The checks cover standard session and Kanban paths, not custom database locations.

## Development and verification

Read `main()` in `hermes-container.sh`, then the named functions in `lib/`.
`lib/config.sh` parses and validates settings. Other libraries handle workspaces,
Podman, services, releases, backups and restore. JSON validators are in `lib/*.jq`.
The separate Python worktree is not part of Hermes Container.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/run.py
```

Tests use temporary files and simulated external services. They do not establish
live Podman, provider, VM-share or recovery compatibility. Full interactive
acceptance on the supported platforms remains outstanding. See
[the behavior and limits](docs/behaviour.md) for the detailed contract.
