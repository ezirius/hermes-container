# One import flow serves explicit ZIP paths and the interactive backup picker.
select_import_backup() {
    local path directory name answer rows='[]' count
    if [[ -n "$IMPORT_ARGUMENT" ]]; then
        path=$IMPORT_ARGUMENT
        [[ "$path" == /* ]] || path=$PWD/$path
        safe_path "$path" file || { fail 4 "backup is missing or unsafe: $path"; return; }
        IMPORT_ARCHIVE=$(canonical_path "$path") || return
        return
    fi
    for path in "$BACKUPS"/*.zip "$BACKUPS"/*/*.zip; do
        exists "$path" || continue
        directory=${path%/*}; name=${path##*/}
        [[ "$name" =~ ^hermes-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}\.zip$ ]] || continue
        if [[ "$directory" != "$BACKUPS" ]]; then
            [[ "${directory##*/}" =~ ^[a-f0-9]{32}$ ]] || continue
        fi
        safe_path "$directory" directory 700 && safe_path "$path" file || continue
        rows=$(printf '%s' "$rows" | json --arg path "$path" --arg name "$name" '.+[{path:$path,name:$name}]') || return
    done
    rows=$(printf '%s' "$rows" | json 'sort_by(.name,.path) | reverse') || return
    count=$(printf '%s' "$rows" | json length)
    ((count>0)) || { fail 4 "no backups found in $BACKUPS; supply a backup ZIP path"; return; }
    printf 'Available backups (newest filename first):\n' >&2
    printf '%s' "$rows" | json -r --arg base "$BACKUPS/" 'to_entries[] | "\(.key+1)) \(.value.path | ltrimstr($base))"' >&2
    select_number 'Select backup' "$count" || return
    answer=$MENU_SELECTION
    IMPORT_ARCHIVE=$(printf '%s' "$rows" | json -r --argjson index "$((answer-1))" '.[$index].path')
}
prepare_import_archive() {
    local path receipt_file= receipt hash source_key selected_key size
    operation_active && safe_path "$IMPORT_ARCHIVE" file || return
    size=$(file_stat size "$IMPORT_ARCHIVE") || return
    require_disk_space "$BACKUPS" "$size" 1 "$RESTORE_RESERVE_GIB" || return
    # Copy once, with cancellation and a timeout. Check room before writing it.
    if ! run_capture "$MAINTENANCE_TIMEOUT" /bin/cp "$IMPORT_ARCHIVE" "$IMPORT_STAGE/input.zip"; then
        operation_active || return
        /bin/cp "$CAPTURE_ERR" "$IMPORT_STAGE/copy.stderr" || return
        fail 3 "could not copy restore archive; inspect $IMPORT_STAGE/copy.stderr (a timeout may leave it empty)"; return
    fi
    operation_active && /bin/chmod 600 "$IMPORT_STAGE/input.zip" || return
    validate_zip "$IMPORT_STAGE/input.zip" || { fail 4 'invalid or unsafe Hermes backup ZIP'; return; }
    # External provider files would land in the disposable container's user home.
    if /usr/bin/grep -Eq '^_external(/|$)' "$SCRATCH/members"; then
        fail 4 'this backup contains external provider files; it needs a manual restore'; return
    fi
    path=${IMPORT_ARCHIVE%/*}
    if exists "${IMPORT_ARCHIVE%.zip}-receipt.json"; then receipt_file=${IMPORT_ARCHIVE%.zip}-receipt.json
    elif [[ "${path##*/}" =~ ^[a-f0-9]{32}$ ]] && exists "$path/receipt.json"; then receipt_file=$path/receipt.json; fi
    if [[ -n "$receipt_file" ]]; then
        receipt=$(read_record "$receipt_file") && validate_receipt "$receipt" || return
        hash=$(file_hash "$IMPORT_STAGE/input.zip") || return
        [[ "$(printf '%s' "$receipt" | json -r .hash)" == "$hash" && "$(printf '%s' "$receipt" | json -r .name)" == "${IMPORT_ARCHIVE##*/}" ]] || {
            fail 4 'backup does not match its receipt'; return
        }
        # A restore into an older image can expose newer data to older migrations.
        source_key=$(release_key "$(printf '%s' "$receipt" | json -r .source.tag)") || return 4
        selected_key=$(release_key "$(printf '%s' "$SELECTED" | json -r .tag)") || return 4
        ((source_key<=selected_key)) || {
            fail 4 'backup was created by a newer Hermes version; select a suitable image before restoring'; return
        }
    else
        warning 'No launcher receipt: ZIP checks cannot establish the backup source or original checksum.'
        confirm 'Continue with this backup without a receipt?' || return
    fi
    check_import_capacity 2 || return
    IMPORT_HASH=$(file_hash "$IMPORT_STAGE/input.zip")
}
check_import_capacity() {
    local copies=$1 size destination=$BACKUPS
    run_capture "$ZIP_LIST_TIMEOUT" /usr/bin/unzip -Z -t "$IMPORT_STAGE/input.zip" || return
    size=$(/usr/bin/awk 'NR==1 {print $3}' "$CAPTURE_OUT")
    # Staging lives in Backups; the final restore may be on a different volume.
    [[ "$copies" != 1 ]] || destination=$HERMES_DATA
    require_disk_space "$destination" "$size" "$copies" "$RESTORE_RESERVE_GIB"
}
verify_import_stage() {
    verify_lock || return
    safe_path "$IMPORT_STAGE" directory 700 && [[ "$(identity "$IMPORT_STAGE")" == "$IMPORT_STAGE_ID" ]] || return 4
    safe_path "$IMPORT_STAGE/home" directory 700 && [[ "$(identity "$IMPORT_STAGE/home")" == "$IMPORT_HOME_ID" ]] || return 4
    safe_path "$IMPORT_STAGE/input.zip" file 600 || return
    [[ "$(file_hash "$IMPORT_STAGE/input.zip")" == "$IMPORT_HASH" ]]
}
# Fresh image startup can warn about its seeded config before import replaces it.
# Allow only that exact warning, and only when the archive's config was restored.
import_output_clean() {
    local data=$1 new_config=$2 stdout=$3 stderr=$4
    native_output_clean "$stdout" "$stderr" && return 0
    [[ "$new_config" == true ]] || return 4
    /usr/bin/awk '
      $0 == "[config-migrate] WARNING: This config predates version 12 (~2 years old) and can no longer be auto-migrated. Back up /opt/data/config.yaml and run `hermes setup` to regenerate, or manually set _config_version: 12 after reviewing the changelog." {next}
      {print}' "$stderr" > "$SCRATCH/import-stderr" || return
    native_output_clean "$stdout" "$SCRATCH/import-stderr" || return
    safe_path "$data/config.yaml" file || return
    run_capture "$ZIP_LIST_TIMEOUT" /usr/bin/unzip -p "$IMPORT_STAGE/input.zip" config.yaml || return
    /usr/bin/cmp -s "$CAPTURE_OUT" "$data/config.yaml"
}
native_import() {
    local data=$1 label=$2 status new_config=false
    verify_import_stage && inventory_guard || return
    exists "$data/config.yaml" || new_config=true
    build_runtime "$SELECTED" import "$data" "$IMPORT_STAGE/input.zip" || return
    RESIDUAL_UNKNOWN=true
    run_capture "$MAINTENANCE_TIMEOUT" "$PODMAN" "${PODMAN_OPTIONS[@]}" "${RUNTIME[@]}"; status=$?
    operation_active || return
    /bin/cp "$CAPTURE_OUT" "$IMPORT_STAGE/$label.stdout" && /bin/cp "$CAPTURE_ERR" "$IMPORT_STAGE/$label.stderr" || return
    printf '%s\n' "$status" > "$IMPORT_STAGE/$label.exit" || return
    inventory_guard || return
    RESIDUAL_UNKNOWN=false
    if ((status!=0)); then
        fail 4 "$label restore command failed (status $status); inspect $IMPORT_STAGE/$label.stderr"; return
    fi
    if ! import_output_clean "$data" "$new_config" "$IMPORT_STAGE/$label.stdout" "$IMPORT_STAGE/$label.stderr" ||
        [[ "$(/usr/bin/grep -Ec '^Import complete: [1-9][0-9]* files restored' "$IMPORT_STAGE/$label.stdout")" != 1 ]]; then
        fail 4 "$label restore reported warnings or incomplete results; inspect $IMPORT_STAGE/$label.stdout and $IMPORT_STAGE/$label.stderr"; return
    fi
}
check_imported_home() {
    # Reuse gateway checks against the isolated result without changing live paths.
    local HERMES_DATA=$1 path members found=false
    check_home_entries "$HERMES_DATA" && gateway_guard || return
    run_capture "$ZIP_LIST_TIMEOUT" /usr/bin/unzip -Z -1 "$IMPORT_STAGE/input.zip" || return
    members=$CAPTURE_OUT
    # Check restored config bytes, not just defaults seeded by the entrypoint.
    for path in config.yaml .env; do
        if /usr/bin/grep -Fxq "$path" "$members"; then
            run_capture "$ZIP_LIST_TIMEOUT" /usr/bin/unzip -p "$IMPORT_STAGE/input.zip" "$path" || return
            /usr/bin/cmp -s "$CAPTURE_OUT" "$HERMES_DATA/$path" || {
                fail 4 "restored $path differs from the selected backup"; return
            }
        fi
    done
    for path in config.yaml .env state.db; do
        if exists "$HERMES_DATA/$path"; then
            safe_path "$HERMES_DATA/$path" file || return
            [[ ! -s "$HERMES_DATA/$path" ]] || found=true
        fi
    done
    $found || { fail 4 'restore produced no non-empty Hermes configuration or database'; return; }
}
operate_import() {
    local current digest entry
    select_workspace && load_workspace && select_import_backup || return
    current=$IMAGE_CONFIG
    inventory_guard && gateway_guard || return
    # Import does not combine recovery with an image update.
    SELECTED=$current
    [[ "$SELECTED" != null ]] || SELECTED=$MIGRATION_IMAGE
    if [[ "$SELECTED" == null ]]; then choose_image null || return
    else
        confirm "Restore using configured/recorded Hermes $(printf '%s' "$SELECTED" | json -r .tag)?" || return
        ALLOW_PULL=true
    fi
    digest=$(printf '%s' "$SELECTED" | json -r .digest)
    host_readiness "$digest" && acquire_lock && revalidate && prepare_directories && verify_vm_shares || return
    ensure_image "$digest" "$ALLOW_PULL" || return
    IMPORT_STAGE=$BACKUPS/.import-$OPERATION_ID
    /bin/mkdir -m 700 "$IMPORT_STAGE" "$IMPORT_STAGE/home" || return
    IMPORT_STAGE_ID=$(identity "$IMPORT_STAGE") || return
    IMPORT_HOME_ID=$(identity "$IMPORT_STAGE/home") || return
    warning "Restore staging is preserved on failure or cancellation: $IMPORT_STAGE"
    prepare_import_archive && native_import "$IMPORT_STAGE/home" staged && check_imported_home "$IMPORT_STAGE/home" || return
    revalidate && verify_import_stage || return
    confirm 'Apply this backup to Hermes Home? Matching files will be overwritten; other files remain.' || return
    entry=$(first_data_entry "$HERMES_DATA" ! -name backups) || return
    if [[ -n "$entry" ]]; then
        filevault_prompt && create_backup "$SELECTED" || return
    fi
    # Staging and the safety backup consumed space; check again before live writes.
    revalidate && verify_import_stage && check_import_capacity 1 || return
    [[ "$current" != null ]] || save_image "$SELECTED" || return
    native_import "$HERMES_DATA" live && check_imported_home "$HERMES_DATA" || {
        operation_active || return
        error "Live restore did not complete cleanly. Keep backups and $IMPORT_STAGE for manual recovery."
        return 4
    }
    verify_import_stage && operation_active || return
    # This is our private temporary copy, never the selected source archive.
    /bin/rm -rf -- "$IMPORT_STAGE" || return
    success 'Hermes backup restored. Start chat when ready.'
}
