validate_record() { printf '%s' "$2" | "$JQ" -e --arg kind "$1" -f "$CODE_DIR/lib/records.jq" >/dev/null; }
# Workspace folders start with one capital; the login name is all lowercase.
workspace_base() { printf '%s\n' "$WORKSPACE_BASE"; }
safe_workspace_base() {
    local path=$1 owner mode
    [[ -d "$path" && ! -L "$path" ]] || {
        fail 4 "workspace base must be an existing directory, not a symlink: $path"; return
    }
    owner=$(file_stat owner "$path") && mode=$(file_stat mode "$path") || return
    [[ "$owner" == 0 || "$owner" == "$OPERATOR_UID" ]] || {
        fail 4 "workspace base must belong to root or the current user (owner UID $owner): $path"; return
    }
    (( (8#$mode & 0002) == 0 )) || {
        fail 4 "workspace base must not be world writable (mode $mode): $path"; return
    }
    if (( (8#$mode & 0020) != 0 )); then
        [[ "$owner" == 0 && "$WORKSPACE_BASE_ALLOW_GROUP_WRITE" == true ]] || {
            fail 4 "group-writable workspace base requires root ownership and WORKSPACE_BASE_ALLOW_GROUP_WRITE=true: $path"; return
        }
    fi
    [[ -r "$path" && -x "$path" ]] || {
        fail 4 "current user needs list and search access to workspace base: $path"; return
    }
}
workspace_username() {
    local username LC_ALL=C
    [[ "$1" =~ ^[A-Z][a-z0-9_-]{0,31}$ ]] || return 2
    username=$(printf '%s' "$1" | LC_ALL=C /usr/bin/tr '[:upper:]' '[:lower:]') || return
    printf '%s\n' "$username"
}
# Add parents from the root outward so mkdir never needs an unchecked -p.
add_source_parents() {
    local root=$1 parent=${2%/*} existing missing=()
    [[ "$root" == /* && "$root" != / && "$root" != */ && "$2" == "$root/"* ]] || {
        fail 4 "source directory must be below its workspace or account home: $2"; return
    }
    while [[ "$parent" != "$root" ]]; do
        missing=("$parent" "${missing[@]}")
        parent=${parent%/*}
    done
    for parent in "${missing[@]}"; do
        for existing in "${SOURCE_PARENTS[@]}"; do
            [[ "$parent" != "$existing" ]] || continue 2
        done
        SOURCE_PARENTS[${#SOURCE_PARENTS[@]}]=$parent
    done
}
set_paths() {
    WORKSPACE=$1
    WORKSPACE_NAME=${WORKSPACE##*/}
    WORKSPACE_USER=$(workspace_username "$WORKSPACE_NAME") || return
    [[ "$WORKSPACE_USER" == "$ACCOUNT_USER" ]] || {
        fail 2 "workspace must match the current account: $ACCOUNT_USER"
        return
    }
    # Read the real account home, including on Linux; never guess it from a name.
    USER_HOME=$ACCOUNT_HOME
    HOME_SOURCE="$WORKSPACE/${HOME_PATH//\{workspace\}/$WORKSPACE_NAME}"
    BACKUPS_SOURCE="$WORKSPACE/${BACKUPS_PATH//\{workspace\}/$WORKSPACE_NAME}"
    DOCS_SOURCE="$WORKSPACE/${AGENT_DOCS_PATH//\{workspace\}/$WORKSPACE_NAME}"
    USER_DOCS_SOURCE="$USER_HOME/${USER_DOCS_PATH//\{workspace\}/$WORKSPACE_NAME}"
    # Podman receives real host paths; container Docs keep their familiar names.
    HERMES_DATA=$(canonical_path "$HOME_SOURCE") || return
    DOCUMENTS=$(canonical_path "$DOCS_SOURCE") || return
    USER_DOCUMENTS=$(canonical_path "$USER_DOCS_SOURCE") || return
    # Docs keep the same absolute paths on the host and inside the container.
    CONTAINER_DOCS=$DOCS_SOURCE
    CONTAINER_USER_DOCS=$USER_DOCS_SOURCE
    SOURCE_PARENTS=()
    add_source_parents "$WORKSPACE" "$HOME_SOURCE" || return
    add_source_parents "$WORKSPACE" "$BACKUPS_SOURCE" || return
    add_source_parents "$WORKSPACE" "$DOCS_SOURCE" || return
    add_source_parents "$USER_HOME" "$USER_DOCS_SOURCE" || return
    SOURCE_PIN_PATHS=(); SOURCE_PIN_IDS=()
    METADATA=$WORKSPACE/.hermesagent
    LEGACY_STATE=$METADATA/bash/state.json
    # A separate host directory keeps archives outside Hermes Home.
    BACKUPS=$(canonical_path "$BACKUPS_SOURCE") || return
    LEGACY_BACKUPS=$METADATA/bash/backups
    CONFIG_BASE=${XDG_CONFIG_HOME:-$ACCOUNT_HOME/.config}
    [[ "$CONFIG_BASE" == /* ]] || { fail 3 "XDG_CONFIG_HOME must be absolute"; return; }
    CONFIG_BASE=${CONFIG_BASE%/}
    CONFIG_DIR=$CONFIG_BASE/hermes
    IMAGE_FILE=$CONFIG_DIR/$WORKSPACE_NAME.json
    LOCK_DIR=$RUNTIME_BASE/hermes
    WORKSPACE_HASH=$(printf '%s' "$WORKSPACE" | sha256 | /usr/bin/awk '{print $1}')
    ACTIVE_LOCK=$LOCK_DIR/$WORKSPACE_HASH.lock
}
select_workspace() {
    local base name username answer path matched=false names=()
    base=$(workspace_base) || return
    safe_workspace_base "$base" || return 3
    for path in "$base"/*; do
        name=${path##*/}
        username=$(workspace_username "$name") && safe_path "$path" directory 2>/dev/null || continue
        [[ "$username" == "$ACCOUNT_USER" ]] || continue
        names[${#names[@]}]=$name
    done
    if [[ -z "$WORKSPACE_ARGUMENT" ]]; then
        ((${#names[@]})) || {
            fail 3 "no eligible workspace in $base for account $ACCOUNT_USER"
            return
        }
        for ((answer=0; answer<${#names[@]}; answer++)); do
            printf '%d) %s\n' "$((answer+1))" "${names[answer]}" >&2
        done
        select_number 'Select workspace' "${#names[@]}" || return
        answer=$MENU_SELECTION
        WORKSPACE_ARGUMENT=${names[answer-1]}
    fi
    # Accept any input case, but use the real directory spelling for every path.
    answer=$(printf '%s' "$WORKSPACE_ARGUMENT" | LC_ALL=C /usr/bin/tr '[:upper:]' '[:lower:]') || return
    for name in "${names[@]}"; do
        if [[ "$(workspace_username "$name")" == "$answer" ]]; then
            WORKSPACE_ARGUMENT=$name
            matched=true
            break
        fi
    done
    $matched || {
        fail 2 "workspace is not eligible: $base/$WORKSPACE_ARGUMENT (check spelling, ownership and account $ACCOUNT_USER)"
        return
    }
    path=$base/$WORKSPACE_ARGUMENT
    safe_path "$path" directory || return
    [[ "$(canonical_path "$path")" == "$path" ]] || return 4
    set_paths "$path" || return
    WORKSPACE_INODE=$(identity "$path")
}
# Follow source directory links, checking the destination's ownership and mode.
safe_source_directory() {
    local resolved
    if [[ ! -L "$1" ]]; then safe_path "$1" directory; return; fi
    resolved=$(canonical_path "$1") || return 4
    safe_path "$resolved" directory
}
source_identity() {
    local resolved
    if [[ ! -L "$1" ]]; then identity "$1"; return; fi
    resolved=$(canonical_path "$1") || return 4
    identity "$resolved"
}
# Recheck both the link destinations and directory identities throughout a run.
verify_source_paths() {
    local path index
    # Sharing permissions can change while a menu or image download is open.
    safe_workspace_base "${WORKSPACE%/*}" || return
    safe_path "$WORKSPACE" directory && safe_path "$USER_HOME" directory || {
        fail 4 "workspace and account home must exist: $WORKSPACE; $USER_HOME"
        return
    }
    for path in "$CONFIG_BASE" "$CONFIG_DIR" "$HERMES_DATA" "$DOCUMENTS" "$USER_DOCUMENTS" "$BACKUPS"; do
        if exists "$path"; then safe_path "$path" directory || return; fi
    done
    for path in "${SOURCE_PARENTS[@]}" "$HOME_SOURCE" "$DOCS_SOURCE" "$USER_DOCS_SOURCE" "$BACKUPS_SOURCE"; do
        if exists "$path"; then safe_source_directory "$path" || return; fi
    done
    [[ "$(canonical_path "$HOME_SOURCE")" == "$HERMES_DATA" &&
       "$(canonical_path "$DOCS_SOURCE")" == "$DOCUMENTS" &&
       "$(canonical_path "$USER_DOCS_SOURCE")" == "$USER_DOCUMENTS" &&
       "$(canonical_path "$BACKUPS_SOURCE")" == "$BACKUPS" ]] || {
        fail 4 'mount source link destination changed'; return
    }
    # Resolved Docs must not expose private Home or become part of its backups.
    for path in "$DOCUMENTS" "$USER_DOCUMENTS"; do
        if [[ "$path" == "$HERMES_DATA" || "$path" == "$HERMES_DATA/"* ||
              "$HERMES_DATA" == "$path/"* || "$path" == / || "$HERMES_DATA" == / ]]; then
            fail 4 "Docs must not overlap Hermes Home: $path"
            return
        fi
    done
    # Backups must stay separate from all data directories, including aliases.
    for path in "$HERMES_DATA" "$DOCUMENTS" "$USER_DOCUMENTS"; do
        if [[ "$BACKUPS" == "$path" || "$BACKUPS" == "$path/"* ||
              "$path" == "$BACKUPS/"* || "$path" == / || "$BACKUPS" == / ]]; then
            fail 4 "Backups must not overlap Home or Docs: $BACKUPS"
            return
        fi
    done
    for ((index=0; index<${#SOURCE_PIN_PATHS[@]}; index++)); do
        [[ "$(source_identity "${SOURCE_PIN_PATHS[index]}")" == "${SOURCE_PIN_IDS[index]}" ]] || {
            fail 4 "mount source directory changed: ${SOURCE_PIN_PATHS[index]}"
            return
        }
    done
}
pin_source_path() {
    local path=$1 current index
    current=$(source_identity "$path") || return
    for ((index=0; index<${#SOURCE_PIN_PATHS[@]}; index++)); do
        if [[ "${SOURCE_PIN_PATHS[index]}" == "$path" ]]; then
            # An existing identity is evidence, never a value to refresh.
            [[ "${SOURCE_PIN_IDS[index]}" == "$current" ]] || return 4
            return 0
        fi
    done
    SOURCE_PIN_PATHS[${#SOURCE_PIN_PATHS[@]}]=$path
    SOURCE_PIN_IDS[${#SOURCE_PIN_IDS[@]}]=$current
}
pin_source_paths() {
    local path
    verify_source_paths || return
    for path in "$CONFIG_BASE" "$CONFIG_DIR" "$WORKSPACE" "$USER_HOME" "${SOURCE_PARENTS[@]}" "$HOME_SOURCE" "$DOCS_SOURCE" "$USER_DOCS_SOURCE" "$BACKUPS_SOURCE" "$HERMES_DATA" "$DOCUMENTS" "$USER_DOCUMENTS" "$BACKUPS"; do
        exists "$path" || continue
        pin_source_path "$path" || return
    done
}
# Finder's view settings are not Hermes data. Preserve a safe regular file, but
# never ignore a symlink or directory merely because it is named .DS_Store.
first_data_entry() {
    local directory=$1; shift
    if exists "$directory/.DS_Store"; then
        safe_path "$directory/.DS_Store" file || return
    fi
    /usr/bin/find "$directory" -mindepth 1 -maxdepth 1 ! -name .DS_Store "$@" -print -quit
}
require_empty_home() {
    [[ -d "$HERMES_DATA" ]] || return 0
    local entry
    # Backups belong beside Home. Preserve any old path for manual migration.
    if exists "$HERMES_DATA/backups"; then
        fail 4 "Home/backups already exists; inspect or migrate it manually before first setup: $HERMES_DATA/backups (use $BACKUPS for backups)"
        return
    fi
    # An unreadable directory is unknown, not empty. Check find's status too.
    entry=$(first_data_entry "$HERMES_DATA") || {
        fail 4 "cannot inspect Hermes Home: $HERMES_DATA"
        return
    }
    if [[ -n "$entry" ]]; then
        # JSON quoting keeps unusual filenames readable without terminal escapes.
        fail 4 "Hermes Home must be empty before first setup; existing entry: $(printf '%s' "$entry" | json -Rs .)"
        return
    fi
    # Existing archives in the separate Backups directory can be restored.
    if exists "$BACKUPS"; then
        safe_path "$BACKUPS" directory 700 || return
    fi
}
# Old records are read only. Never steal an old lock or replay its transaction.
legacy_guard() {
    local path
    exists "$METADATA" || return 0
    safe_path "$METADATA" directory 700 || return
    for path in "$METADATA/config" "$METADATA/state" "$METADATA/locks/operation.json"; do
        exists "$path" && { fail 4 "Python or foreign metadata needs manual migration: $METADATA"; return; }
    done
    for path in "$METADATA/locks" "$METADATA/locks/operation.lock"; do
        if exists "$path"; then safe_path "$path" directory 700 || return; fi
    done
    if exists "$METADATA/locks/operation.lock/active"; then
        fail 4 'the previous launcher has a lock; inspect it before switching launchers'
        return
    fi
    if exists "$LEGACY_BACKUPS"; then
        safe_path "$LEGACY_BACKUPS" directory 700 || return
        local entry
        entry=$(first_data_entry "$LEGACY_BACKUPS") || return
        [[ -z "$entry" ]] || {
            fail 4 "old backups need manual migration; preserved: $LEGACY_BACKUPS"; return
        }
    fi
}
read_image() {
    local value
    if ! exists "$IMAGE_FILE"; then printf 'null\n'; return; fi
    safe_path "$IMAGE_FILE" file 600 || return
    (( $(file_stat size "$IMAGE_FILE") <= MAX_JSON_BYTES )) || return 4
    value=$(strict_json < "$IMAGE_FILE") && validate_record image "$value" || {
        fail 4 "invalid image configuration: $IMAGE_FILE"; return
    }
    release_key "$(printf '%s' "$value" | json -r .tag)" >/dev/null || return 4
    printf '%s\n' "$value"
}
load_workspace() {
    local path legacy
    safe_path "$WORKSPACE" directory || return
    [[ "$(identity "$WORKSPACE")" == "$WORKSPACE_INODE" ]] || return 4
    verify_source_paths && legacy_guard || return
    for path in "$WORKSPACE/hermesagent-home" "$WORKSPACE/hermesagent-docs"; do
        if exists "$path"; then fail 4 "old layout needs manual migration: $path"; return; fi
    done
    # The config base may be new, but its immediate parent must already exist.
    if ! exists "$CONFIG_BASE"; then safe_path "${CONFIG_BASE%/*}" directory || return; fi
    [[ "$(canonical_path "$CONFIG_BASE")" == "$CONFIG_BASE" ]] || { fail 4 "config path must not contain symlinks: $CONFIG_BASE"; return; }
    for path in "$CONFIG_BASE" "$CONFIG_DIR"; do
        if exists "$path"; then
            safe_path "$path" directory || return
            [[ "$(canonical_path "$path")" == "$path" ]] || return 4
        fi
    done
    IMAGE_CONFIG=$(read_image) || return
    MIGRATION_IMAGE=null; LEGACY_RECORD=
    if [[ "$IMAGE_CONFIG" == null ]]; then
        [[ "$ACTION" == setup || "$ACTION" == restore ]] || { fail 4 'run setup first to select an image'; return; }
        if exists "$METADATA"; then
            safe_path "$METADATA/bash" directory 700 || return
            LEGACY_RECORD=$(read_record "$LEGACY_STATE") || return
            printf '%s' "$LEGACY_RECORD" | "$JQ" -e --arg kind state -f "$CODE_DIR/lib/legacy-records.jq" >/dev/null || return 4
            [[ "$(printf '%s' "$LEGACY_RECORD" | json -r .workspace)" == "$WORKSPACE" ]] || return 4
            MIGRATION_IMAGE=$(printf '%s' "$LEGACY_RECORD" | json '(.pending.target // .accepted) | if .==null then null else {tag,platform,digest} end') || return
        fi
        if [[ "$MIGRATION_IMAGE" == null ]]; then
            # Existing backups can be chosen from the first-setup restore menu.
            require_empty_home || return
        fi
    fi
    for path in "$IMAGE_CONFIG" "$MIGRATION_IMAGE"; do
        [[ "$path" == null ]] && continue
        validate_record image "$path" || return 4
        release_key "$(printf '%s' "$path" | json -r .tag)" >/dev/null || return 4
        [[ "$(printf '%s' "$path" | json -r .platform)" == "$NATIVE" ]] || {
            fail 4 'configured image architecture differs from this host'; return
        }
    done
    pin_source_paths
}
acquire_lock() {
    operation_active && legacy_guard || return
    safe_path "$RUNTIME_BASE" directory 700 || return
    private_dir "$LOCK_DIR" || return
    /bin/mkdir -m 700 "$ACTIVE_LOCK" || { fail 4 "operation lock exists: $ACTIVE_LOCK"; return; }
    LOCK_INODE=$(identity "$ACTIVE_LOCK"); LOCK_DIR_INODE=$(identity "$LOCK_DIR")
    RUNTIME_BASE_INODE=$(identity "$RUNTIME_BASE")
    LOCK_TOKEN=$(new_token)
    printf '%s\n' "$LOCK_TOKEN" > "$ACTIVE_LOCK/token" || return
    printf '%s\n' "$$" "$(process_start $$)" > "$ACTIVE_LOCK/owner" || return
    LOCK_HELD=true
}
prepare_directories() {
    local path
    verify_lock || return
    for path in "$CONFIG_BASE" "$CONFIG_DIR" "${SOURCE_PARENTS[@]}" "$HERMES_DATA" "$DOCUMENTS" "$USER_DOCUMENTS" "$BACKUPS"; do
        verify_lock || return
        exists "$path" || /bin/mkdir -m 700 "$path" || return
        safe_source_directory "$path" || return
        pin_source_path "$path" || return
    done
    safe_path "$CONFIG_DIR" directory 700 && safe_path "$BACKUPS" directory 700
}
verify_lock() {
    $LOCK_HELD || return 4
    verify_source_paths && legacy_guard || return
    safe_path "$RUNTIME_BASE" directory 700 && safe_path "$LOCK_DIR" directory 700 && safe_path "$ACTIVE_LOCK" directory 700 || return
    safe_path "$ACTIVE_LOCK/token" file 600 || return
    [[ "$(identity "$RUNTIME_BASE")" == "$RUNTIME_BASE_INODE" && "$(identity "$WORKSPACE")" == "$WORKSPACE_INODE" && "$(identity "$LOCK_DIR")" == "$LOCK_DIR_INODE" && "$(identity "$ACTIVE_LOCK")" == "$LOCK_INODE" && "$(/bin/cat "$ACTIVE_LOCK/token")" == "$LOCK_TOKEN" ]]
}
save_image() {
    local actual
    validate_record image "$1" || return 4
    actual=$(read_image) || return
    [[ "$actual" == "$IMAGE_CONFIG" ]] || { fail 4 'image configuration changed during operation'; return; }
    atomic_record "$IMAGE_FILE" "$1" || return
    IMAGE_CONFIG=$(printf '%s' "$1" | json .)
}
release_lock() {
    $LOCK_HELD || return 0
    if ! $RESIDUAL_UNKNOWN && verify_lock; then
        /bin/rm "$ACTIVE_LOCK/token" "$ACTIVE_LOCK/owner" && /bin/rmdir "$ACTIVE_LOCK" || return
    else warning "Operation lock retained for manual inspection: $ACTIVE_LOCK"; fi
    LOCK_HELD=false
}
