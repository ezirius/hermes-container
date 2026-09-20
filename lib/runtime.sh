podman_machine_name() {
    # Podman 5.8.3 can report an empty DefaultMachine with a valid CurrentMachine.
    json -er '
        .Host | if .DefaultMachine == null or .DefaultMachine == ""
        then .CurrentMachine else .DefaultMachine end
        | select(type=="string" and test("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"))' || {
        fail 3 'Podman machine info must identify a valid default or current machine'; return
    }
}
# Freeze the macOS endpoint so later changes to Podman's default cannot redirect
# a run. Verify its SSH endpoint against the machine used for storage checks.
bind_machine_connection() {
    local machines detail connections endpoint key
    machines=$(podman_json machine info --format json) || return
    BOUND_MACHINE=$(printf '%s' "$machines" | podman_machine_name) || return
    detail=$(podman_json machine inspect "$BOUND_MACHINE") || return
    connections=$(podman_json system connection list --format json) || return
    endpoint=$(printf '%s' "$connections" | json -er --arg name "$BOUND_MACHINE" --argjson machine "$detail" '
        if ($machine|type)!="array" or ($machine|length)!=1
          then error("machine inspection must describe exactly one machine") else . end
        | if $machine[0].Name!=$name then error("inspected machine name differs from the selected machine")
          elif $machine[0].State!="running" then error("selected Podman machine is not running")
          elif $machine[0].Rootful!=false then error("selected Podman machine must use rootless mode (Rootful=false)")
          else . end
        | map(select(.Default==true))
        | if length!=1 then error("default connection") else .[0] end
        | . as $connection
        | .URI | capture("^ssh://(?<user>[A-Za-z0-9_-]+)@127\\.0\\.0\\.1:(?<port>[0-9]+)/run/user/[0-9]+/podman/podman\\.sock$")
        | if .user==$machine[0].SSHConfig.RemoteUsername
             and (.port|tonumber)==$machine[0].SSHConfig.Port
             and $connection.Identity==$machine[0].SSHConfig.IdentityPath
          then $connection.URI else error("connection differs from machine") end') || {
        fail 3 'default Podman connection must use the selected running rootless machine'; return
    }
    key=$(printf '%s' "$detail" | json -er '.[0].SSHConfig.IdentityPath | select(type=="string" and startswith("/"))') || {
        fail 3 'Podman machine must provide an absolute SSH identity path'; return
    }
    PODMAN_OPTIONS=(--url "$endpoint" --identity "$key")
}

# List first, then inspect every ID. Unknown or changing records stop the run.
inventory_one() {
    local tool=$1 listing ids cid raw record records='[]'
    if [[ "$tool" == podman ]]; then
        listing=$(podman_json ps --all --format json) || return
    else
        listing=$(capture_json "$APPLE" list --all --format json) || return
    fi
    ids=$(printf '%s' "$listing" | json -er --arg tool "$tool" --arg mode list \
        --arg cid '' -f "$CODE_DIR/lib/inventory.jq") || return 3
    while IFS= read -r cid; do
        [[ -n "$cid" ]] || continue
        if [[ "$tool" == podman ]]; then
            raw=$(podman_json container inspect "$cid") || return
        else
            raw=$(capture_json "$APPLE" inspect "$cid") || return
        fi
        record=$(printf '%s' "$raw" | json -e --arg tool "$tool" --arg mode inspect \
            --arg cid "$cid" -f "$CODE_DIR/lib/inventory.jq") || return 3
        records=$(printf '%s' "$records" | json --argjson item "$record" '. + [$item]') || return
    done <<< "$ids"
    printf '%s\n' "$records"
}
inventory_snapshot() {
    local values apple='[]'
    values=$(inventory_one podman) || return
    if [[ "$HOST_OS" == Darwin && "$NATIVE" == arm64 && -n "$APPLE" ]]; then apple=$(inventory_one apple) || return; fi
    printf '%s' "$values" | json --argjson apple "$apple" '. + $apple | sort_by(.id)'
}
inventory_guard() {
    local first second source encoded cid directory
    first=$(inventory_snapshot) && second=$(inventory_snapshot) || { fail 3 'inventory unavailable or malformed'; return; }
    [[ "$first" == "$second" ]] || { fail 4 'inventory changed'; return; }
    # Only the persistent-service workflow can exempt its inspected container.
    if [[ -n "${ALLOWED_SERVICE_ID:-}" ]]; then
        first=$(printf '%s' "$first" | json --arg cid "$ALLOWED_SERVICE_ID" 'map(select(.id!=$cid))') || return
    fi
    if [[ -n "${RUN_CID:-}" ]] && exists "$RUN_CID"; then
        safe_path "$RUN_CID" file || return
        (( $(file_stat size "$RUN_CID") <= 65 )) || return 4
        cid=$(/bin/cat "$RUN_CID") || return
        [[ "$cid" =~ ^[a-f0-9]{64}$ ]] || return 4
        printf '%s' "$first" | json -e --arg cid "$cid" 'all(.[]; .id!=$cid)' >/dev/null || { fail 4 'the recorded container still exists'; return; }
    fi
    printf '%s' "$first" | json -e --arg hash "$WORKSPACE_HASH" 'all(.[]; .labels["com.ezirius.hermesagent.workspace_hash"] != $hash)' >/dev/null || {
        fail 4 'a container is already labelled for this Hermes workspace'; return
    }
    # Base64 preserves spaces and punctuation while carrying one path per line.
    while IFS= read -r encoded; do
        [[ -n "$encoded" ]] || continue
        source=$(printf '%s' "$encoded" | json -Rr '@base64d') || return
        for directory in "$HERMES_DATA" "$DOCUMENTS" "$USER_DOCUMENTS" "$BACKUPS"; do
            if same_directory "$source" "$directory"; then
                fail 4 "a container mounts the same Hermes directory: $directory"
                return
            fi
        done
        # Other directories, including parents and children, are allowed.
    done < <(printf '%s' "$first" | json -r '.[].mounts[].source | @base64')
    # No matching directory is success, even when the last comparison was false.
    return 0
}
image_cached() {
    local status
    podman_status image exists "$IMAGE@$1"; status=$?
    operation_active || return
    ((status == 0 || status == 1)) || { fail 3 'image cache inspection failed'; return 3; }
    return "$status"
}
# Cache-only paths must explain a missing image without offering an implicit pull.
require_cached_image() {
    local status
    image_cached "$1"; status=$?
    ((status==0)) && return 0
    if ((status==1)); then
        fail 3 "required image is not cached; no pull was attempted: $IMAGE@$1"
        return
    fi
    return "$status"
}
free_bytes() {
    /bin/df -Pk "$1" | /usr/bin/awk 'NR==2 && NF>=6 && $4 ~ /^[0-9]+$/ {printf "%.0f\n", $4*1024}'
}
# Stay within the exact integer range used by JSON and shell calculations.
# This is a numeric format boundary, not a storage policy or configurable limit.
valid_byte_count() {
    [[ "$1" =~ ^(0|[1-9][0-9]{0,15})$ ]] && (($1<=9007199254740991))
}
require_disk_space() {
    local directory=$1 bytes=$2 copies=$3 reserve_gib=$4 available reserve
    valid_byte_count "$bytes" && [[ "$copies" =~ ^[12]$ && "$reserve_gib" =~ ^[1-9][0-9]{0,5}$ ]] || {
        fail 3 "cannot establish a safe data size for $directory"; return
    }
    available=$(free_bytes "$directory") || {
        fail 3 "cannot read free space in $directory"; return
    }
    valid_byte_count "$available" || { fail 3 "invalid free-space result for $directory"; return; }
    reserve=$((reserve_gib*1073741824))
    # Divide available space instead of multiplying an untrusted archive size.
    ((available>=reserve && bytes<=(available-reserve)/copies)) || {
        fail 3 "insufficient free space in $directory for data plus $reserve_gib GiB reserve"; return
    }
}
host_readiness() {
    local digest=$1 version info client_version engine_version machine_info detail graph backing guest required=$((NEW_IMAGE_FREE_GIB*1073741824)) status quoted
    local required_cpus=$MIN_ENGINE_CPUS required_memory=$MIN_ENGINE_MEMORY_MIB container_memory
    # A custom container allocation can exceed the usual engine minimums.
    ((CONTAINER_CPUS<=required_cpus)) || required_cpus=$CONTAINER_CPUS
    container_memory=$(( $(config_bytes "$CONTAINER_MEMORY") / 1048576 ))
    ((container_memory<=required_memory)) || required_memory=$container_memory
    version=$(podman_json version --format json) || return
    client_version=$(printf '%s' "$version" | json -er '.Client.Version | select(type=="string")') || return 3
    version_at_least "$client_version" "$REQUIRED_PODMAN" || {
        fail 3 "Podman client $REQUIRED_PODMAN or newer is required for $HOST_OS/$NATIVE"; return
    }
    info=$(podman_json info --format json) || return
    engine_version=$(printf '%s' "$info" | json -er '.version.Version | select(type=="string")') || return 3
    version_at_least "$engine_version" "$REQUIRED_PODMAN" || {
        fail 3 "Podman engine $REQUIRED_PODMAN or newer is required; found $engine_version"; return
    }
    printf '%s' "$info" | json -e --arg native "$NATIVE" --argjson cpus "$required_cpus" \
      --argjson memory "$((required_memory*1048576))" '
      .host.os=="linux" and .host.arch==$native
      and .host.cgroupVersion=="v2" and .host.security.rootless==true
      and (.host.cpus|type)=="number" and (.host.cpus|floor)==.host.cpus and .host.cpus>=$cpus
      and (.host.memTotal|type)=="number" and (.host.memTotal|floor)==.host.memTotal
      and .host.memTotal>=$memory' >/dev/null || { fail 3 "engine must be native/rootless Linux, version $REQUIRED_PODMAN or newer, cgroups v2, $required_cpus CPUs and $required_memory MiB"; return; }
    graph=$(printf '%s' "$info" | json -er '.store.graphRoot | select(type=="string" and startswith("/"))') || return 3
    if [[ "$HOST_OS" == Linux ]]; then
        printf '%s' "$info" | json -e '.host.serviceIsRemote==false' >/dev/null || { fail 3 'Linux requires the local Podman engine'; return; }
        safe_path "$graph" directory || return
        guest=$(free_bytes "$graph") || return
        backing=$guest
    else
        machine_info=$(podman_json machine info --format json) || return
        MACHINE=$(printf '%s' "$machine_info" | podman_machine_name) || return
        [[ "$MACHINE" == "${BOUND_MACHINE:-$MACHINE}" ]] || {
            fail 3 'selected Podman machine changed during operation'; return
        }
        detail=$(podman_json machine inspect "$MACHINE") || return
        # jq orders strings and objects above numbers; check types before limits.
        printf '%s' "$detail" | json -e --arg machine "$MACHINE" --argjson cpus "$required_cpus" \
          --argjson memory "$required_memory" '
          type=="array" and length==1 and .[0].Name==$machine
          and .[0].State=="running" and .[0].Rootful==false
          and (.[0].Resources.CPUs|type)=="number"
          and (.[0].Resources.CPUs|floor)==.[0].Resources.CPUs and .[0].Resources.CPUs>=$cpus
          and (.[0].Resources.Memory|type)=="number"
          and (.[0].Resources.Memory|floor)==.[0].Resources.Memory and .[0].Resources.Memory>=$memory
        ' >/dev/null || { fail 3 "Podman machine resources are invalid or below $required_cpus CPUs and $required_memory MiB"; return; }
        # machine ssh crosses a shell boundary. Allow hidden directories such as
        # .local, but refuse shell punctuation and whitespace before passing a path.
        [[ "$graph" =~ ^/([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+/?$ ]] || {
            fail 3 "unsupported Podman guest storage path: $graph"; return
        }
        run_capture "$INSPECT_TIMEOUT" "$PODMAN" machine ssh "$MACHINE" df -Pk "$graph" || {
            fail 3 'could not check free space inside the Podman VM'; return
        }
        guest=$(/usr/bin/awk 'NR==2 && NF==6 && $4 ~ /^[0-9]+$/ {printf "%.0f\n",$4*1024}' "$CAPTURE_OUT")
        backing=$(printf '%s' "$machine_info" | json -er '.Host.MachineImageDir | select(type=="string" and startswith("/"))') || return 3
        backing=$(free_bytes "$backing") || return
    fi
    valid_byte_count "$guest" && valid_byte_count "$backing" || {
        fail 3 'could not read available Podman storage from df output'; return
    }
    image_cached "$digest"; status=$?
    if ((status==0)); then required=$((CACHED_IMAGE_FREE_GIB*1073741824)); elif ((status!=1)); then return "$status"; fi
    ((backing>=required && guest>=required)) || { fail 3 'insufficient Podman storage'; return; }
    if [[ "$HOST_OS" == Darwin ]]; then
        # Remote Podman mounts paths from the VM, not directly from macOS.
        # machine ssh joins arguments into shell text: quote configured paths.
        quoted=${WORKSPACE//\'/\'\\\'\'}
        run_capture "$INSPECT_TIMEOUT" "$PODMAN" machine ssh "$MACHINE" "test -d '$quoted'" || {
            fail 3 "workspace is not accessible inside the Podman VM; check its configured share: $WORKSPACE"
            return
        }
    fi
}
# A directory can exist in the VM without being the Mac's directory. Prove that
# each share exposes a freshly written host file before any image can run.
verify_vm_shares() {
    [[ "$HOST_OS" == Darwin ]] || return 0
    local directory marker marker_id token quoted command status
    operation_active && verify_lock || return
    for directory in "$HERMES_DATA" "$DOCUMENTS" "$USER_DOCUMENTS" "$BACKUPS"; do
        operation_active || return
        marker=$(/usr/bin/mktemp "$directory/.hermes-share.XXXXXXXX") || return
        marker_id=$(identity "$marker") || return
        token=$(new_token) || return
        printf '%s\n' "$token" > "$marker" || return
        # machine ssh joins arguments into a shell command. Quote the whole path,
        # including embedded apostrophes, rather than relying on argv boundaries.
        quoted=${marker//\'/\'\\\'\'}
        command="test \"\$(cat '$quoted')\" = '$token'"
        run_capture "$INSPECT_TIMEOUT" "$PODMAN" machine ssh "$MACHINE" "$command"
        status=$?
        # The guest only reads the marker, so a timed-out read cannot recreate it.
        safe_path "$marker" file 600 && [[ "$(identity "$marker")" == "$marker_id" ]] || return 4
        /bin/rm "$marker" || return
        operation_active || return
        if ((status!=0)); then
            fail 3 "Podman VM cannot read this Mac share; check VM mounts and macOS file access: $directory"
            return
        fi
    done
    verify_lock
}
ensure_image() {
    local digest=$1 allow_pull=$2 status
    operation_active || return
    image_cached "$digest"; status=$?
    ((status==0)) && return 0
    ((status==1)) || return "$status"
    if [[ "$allow_pull" != true ]]; then
        fail 3 "required image is not cached; no pull was attempted: $IMAGE@$digest"
        return
    fi
    verify_lock || return
    run_capture "$PULL_TIMEOUT" "$PODMAN" "${PODMAN_OPTIONS[@]}" pull --tls-verify=true "$IMAGE@$digest"
    status=$?
    # Cancellation is not a pull failure. Otherwise name the failed operation
    # before temporary command output is removed during exit cleanup.
    operation_active || return
    if ((status!=0)); then
        fail "$status" "image pull failed (status $status): $IMAGE@$digest"
        return
    fi
    image_cached "$digest"; status=$?
    if ((status==1)); then
        fail 3 "image pull completed but the required image is not cached: $IMAGE@$digest"
        return
    fi
    return "$status"
}
gateway_guard() {
    local root path record mode=${1:-maintenance}
    if exists "$HERMES_DATA/profiles"; then
        safe_path "$HERMES_DATA/profiles" directory || return
    fi
    # Upstream iterates hidden profile directories too. Never skip their intent.
    for root in "$HERMES_DATA" "$HERMES_DATA"/profiles/* "$HERMES_DATA"/profiles/.[!.]* "$HERMES_DATA"/profiles/..?*; do
        exists "$root" || continue
        [[ -d "$root" || -L "$root" ]] || continue
        safe_path "$root" directory || return
        path=$root/gateway_state.json
        exists "$path" || continue
        safe_path "$path" file || return
        (( $(file_stat size "$path") <= MAX_JSON_BYTES )) || return 4
        record=$(strict_json < "$path") || return 4
        printf '%s' "$record" | json -e --arg mode "$mode" '
          type=="object" and ((if .desired_state!=null then .desired_state else .gateway_state end) as $s
            | ($s|type)=="string" and ((["stopped","startup_failed"] +
              (if $mode=="service" then ["running"] else [] end)) | index($s)!=null))' >/dev/null || { fail 4 'stop Hermes gateways before maintenance; gateway state is active or ambiguous'; return; }
    done
}
dashboard_port() {
    local entry fallback= account entries
    account=$(printf '%s' "$ACCOUNT_USER" | /usr/bin/tr '[:upper:]' '[:lower:]')
    read -r -a entries <<< "$DASHBOARD_PORTS"
    for entry in "${entries[@]}"; do
        if [[ "${entry%:*}" == "$account" ]]; then printf '%s\n' "${entry##*:}"; return; fi
        [[ "${entry%:*}" != default ]] || fallback=${entry##*:}
    done
    printf '%s\n' "$fallback"
}
container_slug() {
    printf '%s' "$WORKSPACE_ARGUMENT" | /usr/bin/tr '[:upper:]' '[:lower:]' | /usr/bin/tr -cd 'a-z0-9' | /usr/bin/cut -c1-24
}
container_name() {
    local tag slug
    tag=$(printf '%s' "$1" | json -r .tag) || return
    slug=$(container_slug) || return
    printf 'hermes-%s-%s-%s-%s-%s\n' "$tag" "$(/bin/date -u +%Y%m%dT%H%M%SZ)" "${slug:-workspace}" "$2" "${WORKSPACE_HASH:0:12}"
}
build_runtime() {
    local image=$1 action=$2 data=$3 output=${4:-} digest tag name run_dir workdir home_mount docs_mount user_docs_mount backup_output=
    operation_active || return
    digest=$(printf '%s' "$image" | json -r .digest); tag=$(printf '%s' "$image" | json -r .tag)
    name=$(container_name "$image" "$action") || return
    run_dir=$(/usr/bin/mktemp -d "$SCRATCH/run.XXXXXXXX") || return
    RUN_CID=$run_dir/cid
    RUNTIME=(run)
    if [[ "$action" == service ]]; then
        SERVICE_NAME=$name
        RUNTIME+=(-d --restart="$RESTART_POLICY")
    else
        RUNTIME+=(--rm)
    fi
    RUNTIME+=(
        --pull=never --image-volume=ignore --http-proxy=false
        --name "$name" --cidfile "$run_dir/cid" --platform "linux/$NATIVE"
        --cpus "$CONTAINER_CPUS" --memory "$CONTAINER_MEMORY" --shm-size "$CONTAINER_SHM_SIZE"
    )
    # The entrypoint starts as namespace root, then drops to HERMES_UID/GID.
    # keep-id makes that final identity map back to the host account on Linux.
    [[ "$HOST_OS" != Linux ]] || RUNTIME+=(--userns=keep-id --user=0:0)
    RUNTIME+=(
        --label "com.ezirius.hermesagent.release=$tag"
        --label "com.ezirius.hermesagent.digest=$digest"
        --label "com.ezirius.hermesagent.workspace_hash=$WORKSPACE_HASH"
        --label "com.ezirius.hermesagent.action=$action"
        --label com.ezirius.hermesagent.managed=true
        --label "com.ezirius.hermesagent.operation=$OPERATION_ID"
    )
    workdir=$CONTAINER_DOCS
    [[ "$action" != backup && "$action" != import ]] || workdir=$CONTAINER_HOME
    RUNTIME+=(
        --env "HERMES_UID=$OPERATOR_UID" --env "HERMES_GID=$OPERATOR_GID"
        --env TERMINAL_ENV=local --env "TERMINAL_CWD=$workdir"
        --env "HERMES_WRITE_SAFE_ROOT=$CONTAINER_DOCS"
    )
    if [[ "$action" == service ]]; then
        # The official image supervises gateway and dashboard together.
        RUNTIME+=(--env HERMES_DASHBOARD=1 --env "HERMES_DASHBOARD_HOST=$DASHBOARD_LISTEN_IP"
            --env "HERMES_DASHBOARD_PORT=$DASHBOARD_CONTAINER_PORT"
            --publish "$DASHBOARD_BIND_IP:$(dashboard_port):$DASHBOARD_CONTAINER_PORT")
    else
        RUNTIME+=(--env HERMES_DASHBOARD=0)
    fi
    home_mount=$(mount_value "$data" "$CONTAINER_HOME") || return
    RUNTIME+=(--mount "$home_mount")
    # Keep Hermes' native backup path, but store it in the separate host Backups.
    # Staged import gets an isolated Home; live import uses persistent Backups too.
    if [[ "$action" != import || "$data" == "$HERMES_DATA" ]]; then
        RUNTIME+=(--mount "$(mount_value "$BACKUPS" "$CONTAINER_BACKUPS")")
    fi
    if [[ "$action" == backup ]]; then
        [[ "$output" == "$BACKUPS/"* ]] || { fail 4 'backup output must be inside Backups'; return; }
        backup_output=$CONTAINER_BACKUPS/${output#"$BACKUPS/"}
        RUNTIME+=(--env "TZ=$MAINTENANCE_TIMEZONE" --env "TMPDIR=$backup_output/tmp" --workdir="$CONTAINER_HOME")
    elif [[ "$action" == import ]]; then
        # Restore has no Docs mounts or network. The input ZIP is read-only.
        RUNTIME+=(--network=none --workdir="$CONTAINER_HOME" --env "TZ=$MAINTENANCE_TIMEZONE"
            --mount "$(mount_value "$output" /import.zip shared ro)")
    else
        # Both Docs sources are mounted; Agent Docs is the default directory.
        docs_mount=$(mount_value "$DOCUMENTS" "$CONTAINER_DOCS" shared) || return
        user_docs_mount=$(mount_value "$USER_DOCUMENTS" "$CONTAINER_USER_DOCS" shared) || return
        # s6 startup splits a working directory containing spaces. Start it in
        # Hermes Home, then change directory after the official privilege drop.
        [[ "$action" == service ]] || RUNTIME+=(-it)
        RUNTIME+=(--mount "$docs_mount" --mount "$user_docs_mount" --workdir="$CONTAINER_HOME")
    fi
    RUNTIME+=("$IMAGE@$digest")
    case "$action" in
        setup|chat)
            # The path stays in the environment, never interpolated as shell code.
            RUNTIME+=(/bin/sh -c 'cd "$TERMINAL_CWD" && exec hermes "$@"' hermes-launcher)
            [[ "$action" != setup ]] || RUNTIME+=(setup)
            ;;
        service) RUNTIME+=(gateway run) ;;
        backup) RUNTIME+=(backup --keep 0 --output "$backup_output") ;;
        import) RUNTIME+=(import /import.zip --force) ;;
        *) return 2 ;;
    esac
}
run_session() {
    local image=$1 action=$2 status
    verify_lock && gateway_guard && inventory_guard || return
    build_runtime "$image" "$action" "$HERMES_DATA" || return
    operation_active || return
    RESIDUAL_UNKNOWN=true
    # Explicitly keep terminal input: asynchronous shell commands otherwise use /dev/null.
    /usr/bin/env -i "${CHILD_ENV[@]}" TERM="${TERM:-$DEFAULT_TERM}" "$PODMAN" "${PODMAN_OPTIONS[@]}" "${RUNTIME[@]}" <&0 &
    CHILD_PID=$!
    wait "$CHILD_PID"; status=$?
    if [[ -n "$INTERRUPTED" ]]; then
        finish_child "$INTERRUPTED"
        status=$((128+INTERRUPTED))
    fi
    CHILD_PID=
    # Cancellation stops new commands; keep the lock for residual inspection.
    [[ -z "$INTERRUPTED" ]] || return "$status"
    if inventory_guard; then RESIDUAL_UNKNOWN=false; else return 4; fi
    return "$status"
}
