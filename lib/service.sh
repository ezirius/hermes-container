# Podman keeps the service record. No extra launcher state file is needed.
service_name() {
    if [[ -n "${SERVICE_NAME:-}" ]]; then printf '%s\n' "$SERVICE_NAME"
    else container_name "$IMAGE_CONFIG" service; fi
}
check_service_image() {
    [[ "$HOST_OS" == Darwin ]] || return 0
    local tag selected minimum
    tag=$(printf '%s' "$1" | json -r .tag) || return
    selected=$(release_key "$tag") && minimum=$(release_key "$MIN_SERVICE_RELEASE") || return
    # Older images can enable WAL on a fresh Mac/VM share before our next check.
    ((selected>=minimum)) || {
        fail 3 "shared service on macOS requires Hermes $MIN_SERVICE_RELEASE or newer; accept an update first"
        return
    }
}
check_service_database() {
    [[ "$HOST_OS" == Darwin ]] || return 0
    local root path mode
    # Existing WAL databases need an offline conversion before concurrent use
    # across the Mac/VM share. Never checkpoint or rewrite user data at startup.
    # Kanban is shared too, including additional named boards.
    for root in "$HERMES_DATA" "$HERMES_DATA"/profiles/* \
        "$HERMES_DATA"/profiles/.[!.]* "$HERMES_DATA"/profiles/..?*; do
        for path in "$root/state.db" "$root/kanban.db" "$root"/kanban/boards/*/kanban.db; do
            exists "$path" || continue
            safe_path "$path" file || return
            [[ -s "$path" ]] || continue
            mode=$(/usr/bin/od -An -j18 -N2 -tu1 "$path" | /usr/bin/tr -s '[:space:]' ' ') || return
            [[ "$mode" != ' 2 2 '* ]] || {
                fail 4 "offline SQLite WAL-to-DELETE conversion is required before shared Mac/VM use: $path (see README)"
                return
            }
        done
    done
}
show_dashboard() {
    printf 'Container: %s\n' "$(service_name)" >&2
    printf 'Dashboard: http://%s:%s\nUse your saved Hermes login.\nClosing chat leaves the service container running.\n\n' "$DASHBOARD_BIND_IP" "$(dashboard_port)" >&2
}
find_service() {
    local listing raw expected digest tag slug
    SERVICE_ID=; SERVICE_STATE=; SERVICE_NAME=; SERVICE_EXECS=0
    # Discover by workspace and role so the creation timestamp survives reuse.
    listing=$(podman_json ps --all --filter "label=com.ezirius.hermesagent.workspace_hash=$WORKSPACE_HASH" \
        --filter label=com.ezirius.hermesagent.action=service --format json) || return
    SERVICE_ID=$(printf '%s' "$listing" | json -er '
      if type!="array" then error("expected a service list")
      elif length==0 then ""
      elif length==1 and (.[0].Id|type)=="string" and (.[0].Id|test("^[a-f0-9]{64}$"))
      then .[0].Id else error("ambiguous service") end') || {
        fail 4 'service inventory is malformed or contains multiple matches; inspect podman ps --all'
        return
    }
    [[ -n "$SERVICE_ID" ]] || return 0
    raw=$(podman_json container inspect "$SERVICE_ID") || return
    digest=$(printf '%s' "$IMAGE_CONFIG" | json -r .digest)
    tag=$(printf '%s' "$IMAGE_CONFIG" | json -r .tag)
    slug=$(container_slug) || return
    expected=$(json -n --arg home "$HERMES_DATA" --arg backups "$BACKUPS" \
        --arg docs "$DOCUMENTS" --arg user "$USER_DOCUMENTS" \
        --arg docs_target "$CONTAINER_DOCS" --arg user_target "$CONTAINER_USER_DOCS" \
        --arg home_target "$CONTAINER_HOME" --arg backups_target "$CONTAINER_BACKUPS" '
        [{Source:$home,Destination:$home_target}, {Source:$backups,Destination:$backups_target},
         {Source:$docs,Destination:$docs_target}, {Source:$user,Destination:$user_target}] | sort_by(.Destination)') || return
    # Never adopt or remove a container just because its name happens to match.
    printf '%s' "$raw" | json -e --arg prefix "hermes-$tag-" \
        --arg suffix "-${slug:-workspace}-service-${WORKSPACE_HASH:0:12}" --arg cid "$SERVICE_ID" \
        --arg image "$IMAGE@$digest" --arg hash "$WORKSPACE_HASH" --arg digest "$digest" \
        --arg uid "$OPERATOR_UID" --arg gid "$OPERATOR_GID" --arg port "$(dashboard_port)" \
        --arg docs_target "$CONTAINER_DOCS" --arg container_port "$DASHBOARD_CONTAINER_PORT" \
        --arg home_target "$CONTAINER_HOME" --arg bind_ip "$DASHBOARD_BIND_IP" \
        --arg listen_ip "$DASHBOARD_LISTEN_IP" \
        --arg restart "$RESTART_POLICY" --argjson cpus "$CONTAINER_CPUS" \
        --argjson memory "$(config_bytes "$CONTAINER_MEMORY")" \
        --argjson shm "$(config_bytes "$CONTAINER_SHM_SIZE")" \
        --argjson mounts "$expected" -f "$CODE_DIR/lib/service.jq" >/dev/null || {
        fail 4 "service container $SERVICE_ID differs from the expected name, image, mounts or settings; inspect it manually"
        return
    }
    SERVICE_STATE=$(printf '%s' "$raw" | json -r '.[0].State.Status') || return
    SERVICE_NAME=$(printf '%s' "$raw" | json -r '.[0].Name') || return
    SERVICE_EXECS=$(printf '%s' "$raw" | json '.[0].ExecIDs // [] | length') || return
}
refresh_service() {
    # Release checks and prompts can take time. Recheck paths, config and the
    # actual container before exempting it from the shared-directory check.
    verify_source_paths || return
    [[ "$(read_image)" == "$IMAGE_CONFIG" ]] || { fail 4 'image configuration changed'; return; }
    find_service || return
    ALLOWED_SERVICE_ID=$SERVICE_ID
    inventory_guard
}
dashboard_authenticated() {
    # Reuse the strict parser: duplicate keys or multiple JSON replies are not
    # evidence that authentication is enabled.
    strict_json < "$1" | json -e 'type=="object" and .auth_required==true
        and (.auth_providers|type)=="array" and (.auth_providers|length)>0
        and all(.auth_providers[]; type=="string" and length>0)' >/dev/null
}
wait_dashboard() {
    local attempt expected_id=$SERVICE_ID
    for ((attempt=0; attempt<DASHBOARD_ATTEMPTS; attempt++)); do
        operation_active || return
        if run_capture "$DASHBOARD_CAPTURE_TIMEOUT" "$CURL" --disable --silent --show-error --fail --noproxy '*' \
            --connect-timeout "$DASHBOARD_CONNECT_TIMEOUT" --max-time "$DASHBOARD_HTTP_TIMEOUT" "http://$DASHBOARD_BIND_IP:$(dashboard_port)/api/status"; then
            if dashboard_authenticated "$CAPTURE_OUT"; then
                # An HTTP response alone does not prove our container survived.
                refresh_service || return
                [[ -n "$SERVICE_ID" && "$SERVICE_ID" == "$expected_id" && "$SERVICE_STATE" == running ]] || {
                    fail 4 'Hermes service changed or stopped while waiting for the dashboard'; return
                }
                return 0
            fi
        fi
        # Preserve cancellation even when there is no next attempt.
        operation_active || return
        # No delay is needed after the last request. Supervise retry sleeps so
        # cancellation can stop them, even when a long interval is configured.
        if ((attempt+1<DASHBOARD_ATTEMPTS)); then
            # One extra second allows for the integer SECONDS clock boundary.
            run_capture "$((DASHBOARD_POLL_SECONDS+1))" /bin/sleep "$DASHBOARD_POLL_SECONDS" || return
        fi
    done
    fail 3 "dashboard is not ready with authentication; inspect: podman logs $(service_name)"
}
start_service() {
    verify_lock && gateway_guard service && inventory_guard && check_service_database || return
    if [[ -n "$SERVICE_ID" ]]; then
        [[ "$SERVICE_STATE" == running ]] && return 0
        [[ "$SERVICE_STATE" == exited || "$SERVICE_STATE" == created ]] || {
            fail 4 "service state needs inspection: $SERVICE_STATE"; return
        }
        RESIDUAL_UNKNOWN=true
        service_command 'start the existing container' "$SERVICE_RESTART_TIMEOUT" start "$SERVICE_ID" || return
    else
        build_runtime "$IMAGE_CONFIG" service "$HERMES_DATA" || return
        RESIDUAL_UNKNOWN=true
        run_capture "$SERVICE_START_TIMEOUT" "$PODMAN" "${PODMAN_OPTIONS[@]}" "${RUNTIME[@]}" || {
            fail 3 "could not start Hermes; inspect: podman ps --all and podman logs $(service_name)"; return
        }
    fi
    find_service || return
    [[ -n "$SERVICE_ID" && "$SERVICE_STATE" == running ]] || {
        fail 3 "Hermes container did not stay running; inspect: podman logs $(service_name)"; return
    }
    # Its continued existence is intentional, not an abandoned temporary run.
    ALLOWED_SERVICE_ID=$SERVICE_ID; RUN_CID=; RESIDUAL_UNKNOWN=false
    inventory_guard
}
# Keep the failed step and its private diagnostics visible. Do not print raw
# container output: it can contain credentials. Cancellation keeps its own status.
service_command() {
    local step=$1 seconds=$2 status
    shift 2
    run_capture "$seconds" "$PODMAN" "${PODMAN_OPTIONS[@]}" "$@"; status=$?
    operation_active || return
    ((status==0)) && return 0
    fail "$status" "could not $step (status $status); inspect $CAPTURE_OUT and $CAPTURE_ERR"
}
stop_service() {
    verify_lock && inventory_guard || return
    [[ -n "$SERVICE_ID" ]] || { success 'Hermes is already stopped.'; return; }
    ((SERVICE_EXECS==0)) || { fail 4 'close active container terminal sessions before stopping Hermes'; return; }
    RESIDUAL_UNKNOWN=true
    if [[ "$SERVICE_STATE" == running ]]; then
        # Stop the dashboard first so it cannot start a gateway during shutdown.
        service_command 'stop the dashboard' "$SERVICE_CONTROL_TIMEOUT" exec "$SERVICE_ID" \
            /command/s6-svc -d /run/service/dashboard || return
        service_command 'wait for the dashboard to stop' "$SERVICE_CONTROL_TIMEOUT" exec "$SERVICE_ID" \
            /command/s6-svwait -d -t "$DASHBOARD_STOP_TIMEOUT_MS" /run/service/dashboard || return
        service_command 'stop the gateways' "$GATEWAY_STOP_TIMEOUT" exec "$SERVICE_ID" \
            hermes gateway stop --all || return
    fi
    # Native stop records stopped intent for every profile. Do not rewrite it.
    gateway_guard || return
    service_command 'stop the container' "$CONTAINER_STOP_TIMEOUT" stop --time "$CONTAINER_STOP_GRACE" "$SERVICE_ID" || return
    service_command 'remove the stopped container' "$SERVICE_CONTROL_TIMEOUT" rm "$SERVICE_ID" || return
    SERVICE_ID=; SERVICE_NAME=; ALLOWED_SERVICE_ID=; RUN_CID=
    inventory_guard || return
    RESIDUAL_UNKNOWN=false
    success 'Hermes stopped. Host data and backups are retained.'
}
service_chat() {
    local status
    verify_lock && inventory_guard || return
    # A signal may arrive during the final checks. Do not open a new chat then.
    operation_active || return
    # exec joins the existing container; closing this session never stops it.
    /usr/bin/env -i "${CHILD_ENV[@]}" TERM="${TERM:-$DEFAULT_TERM}" "$PODMAN" "${PODMAN_OPTIONS[@]}" \
        exec -it --user "$OPERATOR_UID:$OPERATOR_GID" --env "HOME=$CONTAINER_HOME" \
        --workdir "$CONTAINER_DOCS" "$SERVICE_ID" hermes <&0 &
    CHILD_PID=$!
    wait "$CHILD_PID"; status=$?
    if [[ -n "$INTERRUPTED" ]]; then
        finish_child "$INTERRUPTED"
        RESIDUAL_UNKNOWN=true
        status=$((128+INTERRUPTED))
    fi
    CHILD_PID=
    return "$status"
}
operate_service() {
    local current digest original_service changing=false ALLOWED_SERVICE_ID=
    select_workspace && load_workspace && find_service || return
    current=$IMAGE_CONFIG
    original_service=$SERVICE_ID
    ALLOWED_SERVICE_ID=$SERVICE_ID
    inventory_guard || return
    if [[ "$ACTION" == stop ]]; then
        acquire_lock && find_service || return
        ALLOWED_SERVICE_ID=$SERVICE_ID
        stop_service
        return
    fi
    choose_image "$current" || return
    check_service_image "$SELECTED" || return
    if [[ "$SELECTED" != "$current" ]]; then
        changing=true
    fi
    digest=$(printf '%s' "$SELECTED" | json -r .digest)
    host_readiness "$digest" || return
    refresh_service || return
    # Showing the existing dashboard does not need the interactive chat's lock.
    if [[ "$ACTION" == start && "$SERVICE_STATE" == running && "$changing" == false ]]; then
        check_service_database && wait_dashboard && show_dashboard
        return
    fi
    acquire_lock && verify_lock && prepare_directories && verify_vm_shares || return
    [[ "$(read_image)" == "$current" ]] || { fail 4 'image configuration changed'; return; }
    find_service || return
    ALLOWED_SERVICE_ID=$SERVICE_ID
    inventory_guard && gateway_guard service && ensure_image "$digest" "$ALLOW_PULL" || return
    if $changing; then
        # The update choice authorises this sequence. Prepare everything we can
        # before stopping; a failed pull or declined encryption prompt leaves it up.
        ensure_image "$(printf '%s' "$current" | json -r .digest)" false && filevault_prompt || return
        refresh_service && verify_lock || return
        [[ "$SERVICE_ID" == "$original_service" ]] || {
            fail 4 'service changed during upgrade approval; run the command again'; return
        }
        # Native stop checks active exec sessions and preserves host data.
        if [[ -n "$SERVICE_ID" ]]; then stop_service || return; fi
        create_backup "$current" && save_image "$SELECTED" || return
    fi
    start_service && wait_dashboard || return
    show_dashboard
    if $changing; then retain_backups || warning 'Service started; backup cleanup needs inspection.'; fi
    [[ "$ACTION" != chat ]] || service_chat
}
