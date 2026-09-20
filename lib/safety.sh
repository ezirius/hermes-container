# Common file and process operations. All paths are passed as literal arguments.
# Colour only terminal messages. Redirected logs and NO_COLOR stay plain text.
# Leave a blank line after each message for readability.
message() {
    local colour=$1; shift
    if [[ -t 2 && "${TERM:-}" != dumb && -z "${NO_COLOR:-}" ]]; then
        printf '\033[%sm%s\033[0m\n\n' "$colour" "$*" >&2
    else
        printf '%s\n\n' "$*" >&2
    fi
}
success() { message 32 "$*"; }
warning() { message 33 "$*"; }
error() { message 31 "$*"; }
fail() { error "hermes-container.sh: $2"; return "$1"; }
# Compare numeric release components: 1.10 is newer than 1.9.
# Missing minor/patch components mean zero; prereleases are not stable releases.
version_at_least() {
    local actual=$1 minimum=$2 index
    local actual_parts minimum_parts
    [[ "$actual" =~ ^[0-9]{1,6}(\.[0-9]{1,6}){0,2}$ &&
       "$minimum" =~ ^[0-9]{1,6}(\.[0-9]{1,6}){0,2}$ ]] || return 1
    IFS=. read -r -a actual_parts <<< "$actual"
    IFS=. read -r -a minimum_parts <<< "$minimum"
    for index in 0 1 2; do
        (( 10#${actual_parts[index]:-0} > 10#${minimum_parts[index]:-0} )) && return 0
        (( 10#${actual_parts[index]:-0} < 10#${minimum_parts[index]:-0} )) && return 1
    done
    return 0
}
check_jq() {
    local version
    version=$("$JQ" --version) || return 3
    # Apple's suffix identifies its packaged stable build.
    if [[ "$version" == jq-* ]]; then
        local release=${version#jq-}
        version_at_least "${release%-apple}" "$MIN_JQ" && return 0
    fi
    fail 3 "jq $MIN_JQ or newer is required; found $version"
}
json() { "$JQ" -cS "$@"; }
strict_json() { "$JQ" -RscS --argjson max_bytes "$MAX_JSON_BYTES" --argjson max_depth "$MAX_JSON_DEPTH" -f "$CODE_DIR/lib/json.jq"; }
identity() { file_stat identity "$1"; }
sha256() {
    if [[ "$HOST_OS" == Linux ]]; then /usr/bin/sha256sum "$@"; else /usr/bin/shasum -a 256 "$@"; fi
}
# Read stdin so checksum tools cannot prefix the hash when escaping a filename.
file_hash() { sha256 < "$1" | /usr/bin/awk '{print $1}'; }
new_token() { /usr/bin/od -An -N16 -tx1 /dev/urandom | /usr/bin/tr -d ' \n'; }
exists() { [[ -e "$1" || -L "$1" ]]; }
# Call with absolute, normalized paths. Equal paths and either ancestor overlap.
paths_overlap() {
    [[ "$1" == "$2" || "$1" == "$2/"* || "$2" == "$1/"* || "$1" == / || "$2" == / ]]
}
canonical_path() {
    local path=$1 parent tail target count=${2:-0}
    [[ "$path" == /* ]] || return 1
    # Share the traversal limit with parent calls: links can cycle through parents.
    if ((count >= MAX_PATH_STEPS)); then
        fail 4 "path resolution exceeded $MAX_PATH_STEPS steps (possible symlink loop): $path"
        return
    fi
    while [[ -L "$path" ]]; do
        ((count += 1))
        if ((count > MAX_PATH_STEPS)); then
            fail 4 "path resolution exceeded $MAX_PATH_STEPS steps (possible symlink loop): $path"
            return
        fi
        target=$(/usr/bin/readlink "$path") || return
        if [[ "$target" == /* ]]; then path=$target; else path=${path%/*}/$target; fi
    done
    if [[ -d "$path" ]]; then (cd -- "$path" && pwd -P); return; fi
    parent=${path%/*}; tail=${path##*/}; [[ -n "$parent" ]] || parent=/
    [[ "$tail" != . && "$tail" != .. && -n "$tail" ]] || return 1
    parent=$(canonical_path "$parent" "$((count+1))") || return
    printf '%s/%s\n' "${parent%/}" "$tail"
}
safe_path() {
    local path=$1 kind=$2 exact=${3:-} owner mode
    [[ ! -L "$path" ]] || { fail 4 "symlink refused: $path"; return; }
    case "$kind" in directory) [[ -d "$path" ]] ;; file) [[ -f "$path" ]] ;; *) return 4 ;; esac || {
        fail 4 "required $kind is missing, inaccessible or the wrong type: $path"
        return
    }
    owner=$(file_stat owner "$path") || return
    mode=$(file_stat mode "$path") || return
    [[ "$owner" == "$OPERATOR_UID" ]] && (( (8#$mode & 0022) == 0 )) || { fail 4 "unsafe ownership/mode: $path"; return; }
    [[ -z "$exact" || "$mode" == "$exact" ]] || { fail 4 "expected mode $exact: $path"; return; }
}
private_dir() {
    exists "$1" || /bin/mkdir -m 700 -- "$1" || return
    safe_path "$1" directory 700
}
read_record() {
    local path=$1 value raw size
    safe_path "$path" file 600 || return
    size=$(file_stat size "$path") || return
    ((size <= MAX_JSON_BYTES)) || return 4
    raw=$(/bin/cat "$path") || return
    value=$(printf '%s' "$raw" | strict_json) || { fail 4 "invalid JSON record: $path"; return; }
    # Command substitution strips final newlines; compare bytes through a file too.
    printf '%s\n' "$value" > "$SCRATCH/canonical" || return
    if ! /usr/bin/cmp -s "$path" "$SCRATCH/canonical"; then
        printf '%s' "$value" > "$SCRATCH/canonical" || return
        /usr/bin/cmp -s "$path" "$SCRATCH/canonical" || { fail 4 "record must use jq -cS encoding: $path"; return; }
    fi
    printf '%s\n' "$value"
}
atomic_record() {
    local path=$1 value=$2 parent temporary before old_destination absent=false
    operation_active || return
    verify_lock || return
    parent=${path%/*}; safe_path "$parent" directory 700 || return
    before=$(identity "$parent") || return
    if exists "$path"; then
        safe_path "$path" file 600 || return
        old_destination=$(identity "$path") || return
    else absent=true; old_destination=; fi
    temporary=$(/usr/bin/mktemp "$parent/.write.XXXXXXXX") || return
    printf '%s' "$value" | strict_json > "$temporary" || return
    /bin/chmod 600 "$temporary" && /bin/sync || return
    [[ "$(identity "$parent")" == "$before" ]] || return 4
    verify_lock || return
    # Verification can be interrupted too. Recheck before publishing any state.
    operation_active || return
    if $absent; then
        # A hard link cannot replace a concurrently-created destination.
        /bin/ln "$temporary" "$path" || return
        /bin/rm "$temporary" || return
    else
        [[ "$(identity "$path")" == "$old_destination" ]] || return 4
        /bin/mv -f "$temporary" "$path" || return
    fi
    /bin/sync
}
# All numbered menus share the same retry and exit behaviour.
select_number() {
    local label=$1 count=$2 answer
    while :; do
        operation_active || return
        printf '%s (1-%s, q to quit): ' "$label" "$count" >&2
        IFS= read -r answer || return 130
        operation_active || return
        case "$answer" in q|Q) return 130 ;; esac
        if [[ "$answer" =~ ^[0-9]{1,6}$ ]]; then
            answer=$((10#$answer))
            if ((answer>=1 && answer<=count)); then
                MENU_SELECTION=$answer
                return 0
            fi
        fi
        warning "Enter a number from 1 to $count, or q to quit."
    done
}
confirm() {
    local answer
    operation_active || return
    printf '%s [y/N] ' "$1" >&2
    IFS= read -r answer || return 130
    # A signal can arrive as read completes; it still cancels the operation.
    operation_active || return
    case "$answer" in y|Y|yes|YES) return 0 ;; *) return 130 ;; esac
}
# Poll a child we own; never send signals to a PID obtained from workspace data.
process_start() { /bin/ps -p "$1" -o lstart= 2>/dev/null || printf 'unavailable\n'; }
signal_child() {
    local line job pid
    [[ -n "${CHILD_PID:-}" ]] || return 0
    # Bash owns this job table. Signal its job, rather than a numeric PID that
    # might have been reused, and do not depend on permission to run ps.
    while IFS= read -r line; do
        if [[ "$line" =~ ^\[([0-9]+)\][+-]?[[:space:]]+([0-9]+)[[:space:]] ]]; then
            job=${BASH_REMATCH[1]}; pid=${BASH_REMATCH[2]}
            if [[ "$pid" == "$CHILD_PID" ]]; then
                kill -"$1" "%$job" 2>/dev/null || :
                return 0
            fi
        fi
    done < <(jobs -l)
}
# Reap only our own job. A child that ignores cancellation must not hold the
# launcher open until a long maintenance timeout expires.
finish_child() {
    signal_child "$1"
    /bin/sleep "$CHILD_TERM_GRACE_SECONDS"
    signal_child KILL
    wait "$CHILD_PID" 2>/dev/null
    CHILD_PID=
}
interrupted() { INTERRUPTED=$1; signal_child "$1"; }
operation_active() {
    [[ -z "${INTERRUPTED:-}" ]] || return "$((128 + INTERRUPTED))"
}
capture_within_limits() {
    local out_size err_size
    out_size=$(file_stat size "$CAPTURE_OUT") && err_size=$(file_stat size "$CAPTURE_ERR") || return 3
    ((out_size <= MAX_STDOUT_BYTES && err_size <= MAX_STDERR_BYTES))
}
run_capture() {
    local seconds=$1; shift
    local start=$SECONDS status
    operation_active || return
    CAPTURE_NUMBER=$((CAPTURE_NUMBER + 1))
    CAPTURE_OUT=$SCRATCH/out.$CAPTURE_NUMBER
    CAPTURE_ERR=$SCRATCH/err.$CAPTURE_NUMBER
    : > "$CAPTURE_OUT" && : > "$CAPTURE_ERR" || return
    /usr/bin/env -i "${CHILD_ENV[@]}" "$@" >"$CAPTURE_OUT" 2>"$CAPTURE_ERR" &
    CHILD_PID=$!
    while kill -0 "$CHILD_PID" 2>/dev/null; do
        if [[ -n "$INTERRUPTED" ]] || ((SECONDS-start >= seconds)) || ! capture_within_limits; then
            finish_child "${INTERRUPTED:-TERM}"
            operation_active || return
            return 3
        fi
        /bin/sleep "$PROCESS_POLL_SECONDS"
    done
    wait "$CHILD_PID"; status=$?; CHILD_PID=
    [[ -z "$INTERRUPTED" ]] || return "$((128 + INTERRUPTED))"
    # A fast child may finish between polls. Check both files once more.
    capture_within_limits || return 3
    return "$status"
}
capture_json() {
    if ! run_capture "$INSPECT_TIMEOUT" "$@"; then
        # Keep a user's cancellation distinct from an inspection failure.
        operation_active || return
        fail 3 'external inspection failed'
        return
    fi
    strict_json < "$CAPTURE_OUT"
}
# These adapters are replaced by tests, not by production environment switches.
podman_json() { capture_json "$PODMAN" "${PODMAN_OPTIONS[@]}" "$@"; }
podman_status() { run_capture "$INSPECT_TIMEOUT" "$PODMAN" "${PODMAN_OPTIONS[@]}" "$@"; }
same_directory() {
    local first second
    first=$(canonical_path "$1") && second=$(canonical_path "$2") || return 0
    # Compare the directory itself, not its parents or children.
    [[ "$first" == "$second" ]] && return 0
    [[ -e "$first" && -e "$second" && "$(identity "$first")" == "$(identity "$second")" ]]
}
csv_field() {
    local value=$1
    value=${value//\"/\"\"}
    printf '"%s"' "$value"
}
mount_value() {
    # Both paths are CSV fields; either can contain a comma or quotation mark.
    printf 'type=bind,%s,%s,%s' "$(csv_field "source=$1")" "$(csv_field "target=$2")" "${4:-rw}"
    # Home stays private; Docs use shared labels so other containers retain access.
    [[ "$HOST_OS" != Linux ]] || printf ',relabel=%s' "${3:-private}"
}
