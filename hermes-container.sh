#!/bin/bash
# A small Podman launcher. Read main() first, then the named steps in lib/.
# The Python implementation is independent and is not imported by this script.
set -o pipefail
CODE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || exit 3
TRUSTED_PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
IMAGE=docker.io/nousresearch/hermes-agent
for library in platform safety workspace runtime releases backups imports service; do
    source "$CODE_DIR/lib/$library.sh" || exit 3
done
usage() {
    /bin/cat <<'USAGE'
Usage: hermes-container.sh [start|chat|stop|setup|backup] [workspace]
       hermes-container.sh import [workspace] [backup.zip]
       hermes-container.sh --help|-h

No arguments: start or reuse Hermes, then open chat in its container.
setup: configure Hermes; first setup offers import or new setup.
start: start or reuse the shared service, then show its name and dashboard URL.
stop: stop gateways and remove the service container; keep all host data.
backup: create a native Hermes backup using the configured image.
import: restore a ZIP; omit its path to choose from saved backups.
Workspace names accept any case. Omit the name to select a workspace.

USAGE
}
parse_args() {
    local LC_ALL=C
    ACTION=chat
    WORKSPACE_ARGUMENT=
    IMPORT_ARGUMENT=
    if (($#==0)); then return 0; fi
    if (($#==1)); then
        case "$1" in
            --help|-h) ACTION=help; return ;;
        esac
    fi
    ACTION=$1
    case "$ACTION" in
        start|setup|chat|stop|backup) (($#<=2)) || return 2 ;;
        import) (($#<=3)) || return 2 ;;
        *) return 2 ;;
    esac
    WORKSPACE_ARGUMENT=${2:-}
    IMPORT_ARGUMENT=${3:-}
    # A lone ZIP path selects the workspace interactively.
    if [[ "$ACTION" == import && $# == 2 && ( "$2" == */* || "$2" == *.zip ) ]]; then
        IMPORT_ARGUMENT=$2; WORKSPACE_ARGUMENT=
    elif (($#>=2)); then
        # Reject options and malformed names before checking tools or the host.
        [[ "$2" =~ ^[A-Za-z][A-Za-z0-9_-]{0,31}$ ]] || return 2
    fi
    (($#<3)) || [[ -n "$3" ]] || return 2
}
find_tool() {
    local name=$1 directory candidate owner mode
    for directory in /opt/homebrew/bin /usr/local/bin /usr/bin /bin /usr/sbin /sbin; do
        candidate=$directory/$name
        [[ -x "$candidate" && -f "$candidate" ]] || continue
        candidate=$(canonical_path "$candidate") || return
        owner=$(file_stat owner "$candidate"); mode=$(file_stat mode "$candidate")
        [[ "$owner" == 0 || "$owner" == "$OPERATOR_UID" ]] && (( (8#$mode & 0022)==0 )) || continue
        printf '%s\n' "$candidate"; return
    done
    return 3
}
initialise() {
    local os_version=
    [[ "$HOST_OS" != Darwin ]] || os_version=$(/usr/bin/sw_vers -productVersion)
    platform_rules "$HOST_OS" "$(/usr/bin/uname -m)" "$os_version" || return
    [[ -t 0 && -t 1 ]] || { fail 2 'launcher operations require a terminal'; return; }
    umask 077
    export PATH=$TRUSTED_PATH
    OPERATOR_UID=$(/usr/bin/id -u); OPERATOR_GID=$(/usr/bin/id -g)
    ((OPERATOR_UID>0)) || { fail 3 'run as your ordinary account'; return; }
    ACCOUNT_USER=$(/usr/bin/id -un "$OPERATOR_UID") || return
    ACCOUNT_HOME=$(account_home) || return
    # macOS uses /Users/<lowercase-login>; do not invent or rename an account home.
    if [[ "$HOST_OS" == Darwin && "$ACCOUNT_HOME" != "/Users/$ACCOUNT_USER" ]]; then
        fail 3 "account home must be /Users/$ACCOUNT_USER: $ACCOUNT_HOME"
        return
    fi
    [[ "$ACCOUNT_HOME" == /* && "$ACCOUNT_HOME" != *$'\n'* ]] && safe_path "$ACCOUNT_HOME" directory || return 3
    JQ=$(find_tool jq) && CURL=$(find_tool curl) && PODMAN=$(find_tool podman) || { fail 3 'jq, curl and Podman are required'; return; }
    [[ -x /usr/bin/unzip ]] || { fail 3 'unzip is required'; return; }
    check_jq || return
    child_environment || return
    if [[ "$HOST_OS" == Linux ]]; then
        RUNTIME_BASE=$RUNTIME_DIR
    else
        RUNTIME_BASE=${XDG_RUNTIME_DIR:-$(/usr/bin/getconf DARWIN_USER_TEMP_DIR)}
        RUNTIME_BASE=$(canonical_path "$RUNTIME_BASE") || {
            fail 3 'a private absolute runtime directory is required'; return
        }
    fi
    safe_path "$RUNTIME_BASE" directory 700 || { fail 3 "private runtime directory unavailable: $RUNTIME_BASE"; return; }
    APPLE=
    [[ "$HOST_OS" == Darwin && "$NATIVE" == arm64 ]] && APPLE=$(find_tool container) || :
    TEMP_BASE=/tmp
    [[ "$HOST_OS" != Darwin ]] || TEMP_BASE=/private/tmp
    SCRATCH=$(/usr/bin/mktemp -d "$TEMP_BASE/hermesagent.XXXXXXXX") || return
    SCRATCH_INODE=$(identity "$SCRATCH") || return
    CAPTURE_NUMBER=0; CHILD_PID=; INTERRUPTED=
    LOCK_HELD=false; RESIDUAL_UNKNOWN=false
    OPERATION_ID=$(new_token)
    trap 'interrupted 2' INT
    trap 'interrupted 15' TERM
    trap cleanup EXIT
    [[ "$HOST_OS" != Darwin ]] || bind_machine_connection
}
cleanup() {
    local status=$?
    [[ -z "${CHILD_PID:-}" ]] || { signal_child TERM; RESIDUAL_UNKNOWN=true; }
    release_lock || warning 'Lock cleanup needs inspection.'
    # Preserve diagnostics and CID files when container cleanup is uncertain.
    if $RESIDUAL_UNKNOWN; then warning "Diagnostics preserved: $SCRATCH"
    elif [[ "$SCRATCH" == "$TEMP_BASE"/hermesagent.* && -d "$SCRATCH" && ! -L "$SCRATCH" && "$(identity "$SCRATCH")" == "$SCRATCH_INODE" ]]; then
        /bin/rm -rf -- "$SCRATCH"
    fi
    return "$status"
}
filevault_prompt() {
    if [[ "$HOST_OS" == Linux ]]; then
        confirm 'Backups may contain plaintext credentials; disk encryption is not checked on Linux. Continue?'
        return
    fi
    run_capture 10 /usr/bin/fdesetup status
    if [[ $? != 0 ]] || ! /usr/bin/grep -Fxq 'FileVault is On.' "$CAPTURE_OUT"; then
        confirm 'FileVault is off or unknown; backups can contain plaintext credentials. Continue?' || return
    fi
}
revalidate() {
    local saved_image=$IMAGE_CONFIG saved_legacy=$LEGACY_RECORD
    verify_lock && load_workspace || return
    [[ "$saved_image" == "$IMAGE_CONFIG" && "$saved_legacy" == "$LEGACY_RECORD" ]] || {
        fail 4 'image configuration or legacy records changed'; return
    }
    gateway_guard && inventory_guard
}
# First setup offers the same importer as the explicit import command.
choose_setup_action() {
    operation_active || return
    printf '1) Import a backup\n2) Set up new Hermes\n' >&2
    select_number 'Select option' 2 || return
    case "$MENU_SELECTION" in
        1) ACTION=import ;;
        2) : ;;
    esac
}
operate() {
    local current before digest status changing=false
    select_workspace && load_workspace || return
    current=$IMAGE_CONFIG
    if [[ "$ACTION" == setup && "$current" == null && "$MIGRATION_IMAGE" == null ]]; then
        choose_setup_action || return
        if [[ "$ACTION" == import ]]; then operate_import; return; fi
    fi
    inventory_guard && gateway_guard || return
    if [[ "$MIGRATION_IMAGE" != null ]]; then
        warning 'Old records are preserved. This selects their image; it does not replay or repair the old operation.'
        confirm "Use recorded $(printf '%s' "$MIGRATION_IMAGE" | json -r .tag) for manual setup?" || return
        SELECTED=$MIGRATION_IMAGE; ALLOW_PULL=true
    else
        choose_image "$current" || return
    fi
    digest=$(printf '%s' "$SELECTED" | json -r .digest)
    host_readiness "$digest" || return
    acquire_lock && revalidate && prepare_directories && verify_vm_shares || return
    ensure_image "$digest" "$ALLOW_PULL" || return
    [[ "$ACTION" != setup && "$SELECTED" == "$current" ]] || changing=true
    if $changing; then
        before=$current
        [[ "$before" != null ]] || before=$MIGRATION_IMAGE
        if [[ "$before" != null ]]; then
            ensure_image "$(printf '%s' "$before" | json -r .digest)" false || return
            confirm 'Create a verified native Hermes backup before setup/update?' && filevault_prompt || return
            create_backup "$before" || return
        fi
        revalidate || return
        # Save the desired image BEFORE it can change Home. A failure must not
        # silently select the previous image against potentially newer data.
        save_image "$SELECTED" || return
    fi
    run_session "$SELECTED" "$ACTION"; status=$?
    operation_active || return
    if ((status!=0)); then
        error 'Hermes failed. The selected image and any backup are retained; recovery is manual.'
        return "$status"
    fi
    success 'Hermes session completed.'
    if $changing; then
        retain_backups || warning 'Session succeeded; backup cleanup needs manual inspection.'
    fi
}
main() {
    local status
    parse_args "$@" || {
        error 'Error: invalid arguments. Use one of the forms below.'
        usage >&2
        return 2
    }
    if [[ "$ACTION" == help ]]; then usage; return; fi
    initialise && {
        case "$ACTION" in
            backup) operate_backup ;;
            import) operate_import ;;
            start|chat|stop) operate_service ;;
            *) operate ;;
        esac
    }
    status=$?
    # Helpers may classify a failed command, but cancellation takes precedence.
    operation_active || return
    return "$status"
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"; result=$?
    exit "$result"
fi
