# Hermes creates archives. The host verifies them and applies the configured retention period.
validate_zip() {
    local archive=$1 name member previous= found=false
    operation_active || return
    safe_path "$archive" file 600 || return
    [[ -s "$archive" ]] || return 4
    run_capture "$ZIP_TEST_TIMEOUT" /usr/bin/unzip -t -P '' "$archive" || return 4
    run_capture "$ZIP_LIST_TIMEOUT" /usr/bin/unzip -Z -v "$archive" || return 4
    /usr/bin/grep -Eq 'file security status:[[:space:]]+encrypted' "$CAPTURE_OUT" && return 4
    run_capture "$ZIP_LIST_TIMEOUT" /usr/bin/unzip -Z -l "$archive" || return 4
    /usr/bin/grep -Eq '^[lbcps][rwxstST-]{9}[[:space:]]' "$CAPTURE_OUT" && return 4
    run_capture "$ZIP_LIST_TIMEOUT" /usr/bin/unzip -Z -1 "$archive" || return 4
    /usr/bin/sort "$CAPTURE_OUT" > "$SCRATCH/members" || return
    while IFS= read -r name; do
        operation_active || return
        [[ -n "$name" && "$name" != /* && "$name" != *\\* && "$name" != "$previous" ]] || return 4
        member=${name%/}
        # Native Hermes must exclude its backups directory, including old archives.
        case "$name" in backups|backups/*) return 4 ;; esac
        case "/$member/" in */../*|*/./*|*//* ) return 4 ;; esac
        case "$name" in config.yaml|.env|state.db) found=true ;; esac
        previous=$name
    done < "$SCRATCH/members"
    # Check Unicode names together, without starting jq once for every file.
    # A valid ZIP can still be impossible to restore: a file cannot also be
    # a directory or the parent of another member (for example, config.yaml/x).
    json -Rse '
        split("\n") | map(select(length > 0)) as $names
        | (reduce $names[] as $name ({}; .[$name] = true)) as $entries
        | all($names[]; test("[\\p{Cc}\\p{Cf}\\p{Cs}\\p{Zl}\\p{Zp}]") | not)
        and all($names[]; split("/") as $parts
            | all(range(1; $parts | length);
                  $entries[$parts[:.] | join("/")] | not))
    ' "$SCRATCH/members" >/dev/null || return 4
    $found
}
# Ignore only the observed s6 shutdown notice, never Hermes backup warnings.
native_output_clean() {
    /usr/bin/awk '
      $0 == "s6-rc: warning: service s6rc-oneshot-runner is marked as essential, not stopping it" {next}
      tolower($0) ~ /warning|incomplete|error:|failed|files skipped|no files to back up/ {bad=1}
      END {exit bad ? 1 : 0}' "$@"
}
backup_output_ok() {
    local directory=$1 name=$2 destination=$3
    safe_path "$directory/exit" file 600 &&
        safe_path "$directory/stdout" file 600 &&
        safe_path "$directory/stderr" file 600 || return
    [[ "$(/bin/cat "$directory/exit")" == 0 ]] || return 4
    [[ "$(/usr/bin/grep -c '^Backup complete: ' "$directory/stdout")" == 1 ]] || return 4
    /usr/bin/grep -Fx "Backup complete: $destination/$name" "$directory/stdout" >/dev/null || return 4
    # Native exclusions are normal. Copy warnings and incomplete output are not.
    native_output_clean "$directory/stdout" "$directory/stderr"
}
check_home_entries() {
    local root=$1 path
    # Inspect real filenames: unzip listings can hide control characters.
    # NUL separators preserve spaces, newlines and Unicode without ambiguity.
    # Compare literally: -path would treat brackets in Home as wildcards.
    /usr/bin/find "$root" \
        -name backups -exec /bin/test '{}' = "$root/backups" \; -prune \
        -o -print0 > "$SCRATCH/source-names" || return
    # Check the whole list once rather than starting jq for every file.
    json -Rse 'split("\u0000") | all(.[];
        test("[\\p{Cc}\\p{Cf}\\p{Cs}\\p{Zl}\\p{Zp}]") | not)' "$SCRATCH/source-names" >/dev/null || {
        fail 4 'control character in a Hermes Home filename'; return
    }
    while IFS= read -r -d '' path; do
        [[ -f "$path" || -d "$path" || -L "$path" ]] || { fail 4 'special file in Hermes Home'; return; }
    done < "$SCRATCH/source-names"
}
check_backup_sources() {
    local allocated logical stat_args du_args
    check_home_entries "$HERMES_DATA" || return
    # Native Hermes excludes backups; do not count old archives as new input.
    # Run du inside Home so its exclusion pattern contains no host-path wildcards.
    if [[ "$HOST_OS" == Linux ]]; then du_args=(--exclude=./backups); else du_args=(-I backups); fi
    allocated=$(cd -- "$HERMES_DATA" &&
        /usr/bin/du -sk "${du_args[@]}" . |
        /usr/bin/awk '{printf "%.0f\n",$1*1024}') || return
    if [[ "$HOST_OS" == Linux ]]; then stat_args=(-c '%s' --); else stat_args=(-f '%z'); fi
    logical=$(/usr/bin/find "$HERMES_DATA" \
        -name backups -exec /bin/test '{}' = "$HERMES_DATA/backups" \; -prune \
        -o -type f -exec /usr/bin/stat "${stat_args[@]}" {} + |
        /usr/bin/awk '{total+=$1} END {printf "%.0f\n",total}') || return
    valid_byte_count "$allocated" && valid_byte_count "$logical" || {
        fail 3 'cannot establish a safe backup source size'; return
    }
    ((allocated>logical)) && logical=$allocated
    require_disk_space "$BACKUPS" "$logical" 2 "$BACKUP_RESERVE_GIB"
}
native_backup() {
    build_runtime "$1" backup "$HERMES_DATA" "$2" || return
    run_capture "$MAINTENANCE_TIMEOUT" "$PODMAN" "${PODMAN_OPTIONS[@]}" "${RUNTIME[@]}"
}
# Publish each verified backup immediately. Failed setup does not hide its backup
# behind a transaction journal, and failed captures stay separate for inspection.
create_backup() {
    local source=$1 directory final receipt_file archive name hash receipt status count=0
    verify_lock && gateway_guard && inventory_guard && check_backup_sources || return
    directory=$BACKUPS/.pending-$OPERATION_ID
    /bin/mkdir -m 700 "$directory" && /bin/mkdir -m 700 "$directory/tmp" || return
    RESIDUAL_UNKNOWN=true
    native_backup "$source" "$directory"; status=$?
    operation_active || return
    /bin/cp "$CAPTURE_OUT" "$directory/stdout" && /bin/cp "$CAPTURE_ERR" "$directory/stderr" || return
    printf '%s\n' "$status" > "$directory/exit" || return
    inventory_guard || return 4
    RESIDUAL_UNKNOWN=false
    if ((status!=0)); then
        fail "$status" "backup command failed; capture preserved: $directory"; return
    fi
    for archive in "$directory"/*.zip; do
        [[ -f "$archive" && ! -L "$archive" ]] || continue
        count=$((count+1)); name=${archive##*/}
    done
    ((count==1)) && [[ "$name" =~ ^hermes-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}\.zip$ ]] || {
        fail 4 "expected one native backup ZIP; capture preserved: $directory"; return
    }
    archive=$directory/$name
    /bin/chmod 600 "$archive" || return
    backup_output_ok "$directory" "$name" "$CONTAINER_BACKUPS/${directory#"$BACKUPS/"}" && validate_zip "$archive" || {
        fail 4 "backup verification failed; capture preserved: $directory"; return
    }
    hash=$(file_hash "$archive") || return
    receipt=$(json -n --arg workspace "$WORKSPACE" --arg operation "$OPERATION_ID" --arg name "$name" --arg hash "$hash" --arg captured "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson source "$source" '{schema:2,workspace:$workspace,operation:$operation,name:$name,hash:$hash,captured:$captured,source:$source}') || return
    validate_record receipt "$receipt" && atomic_record "$directory/receipt.json" "$receipt" || return
    verify_lock || return
    final=$BACKUPS/$name
    receipt_file=$BACKUPS/${name%.zip}-receipt.json
    # Same-second names must never overwrite an earlier backup or receipt.
    if exists "$final" || exists "$receipt_file"; then
        fail 4 "backup filename already exists; capture preserved: $directory"; return
    fi
    operation_active || return
    /bin/ln "$archive" "$final" && /bin/ln "$directory/receipt.json" "$receipt_file" && /bin/sync || return
    success "Verified backup: $final"
    operation_active || return
    # Remove only known capture files. Unexpected leftovers remain for inspection.
    /bin/rm "$archive" "$directory/receipt.json" "$directory/stdout" "$directory/stderr" "$directory/exit" || return
    /bin/rmdir "$directory/tmp" "$directory" || warning "Extra capture files preserved: $directory"
}
# Old receipts remain verifiable; they are never rewritten into the new format.
validate_receipt() {
    if [[ "$(printf '%s' "$1" | json .schema)" == 1 ]]; then
        printf '%s' "$1" | "$JQ" -e --arg kind receipt -f "$CODE_DIR/lib/legacy-records.jq" >/dev/null || return 4
    else validate_record receipt "$1" || return 4; fi
    # The schema checks the tag's shape; also reject impossible release dates.
    release_key "$(printf '%s' "$1" | json -r .source.tag)" >/dev/null || return 4
}
backup_cutoff() {
    local now
    now=$(/bin/date -u +%s) || return
    [[ "$now" =~ ^[0-9]+$ ]] || return 3
    printf '%s\n' "$((now - BACKUP_RETENTION_DAYS*86400))"
}
retain_backups() {
    local entry directory receipt_file receipt name hash before cutoff candidate candidates='[]' encoded unverified=false
    verify_lock || return
    cutoff=$(backup_cutoff) || return
    for entry in "$BACKUPS"/*; do
        exists "$entry" || continue
        directory=$BACKUPS
        case "${entry##*/}" in
            hermes-backup-*-receipt.json) receipt_file=$entry ;;
            hermes-backup-*.zip)
                # Its receipt is checked separately; preserve orphan archives.
                exists "${entry%.zip}-receipt.json" || unverified=true
                continue ;;
            *)
                # Read old operation directories until their backups expire.
                [[ "${entry##*/}" =~ ^[a-f0-9]{32}$ ]] || { unverified=true; continue; }
                safe_path "$entry" directory 700 || { unverified=true; continue; }
                directory=$entry; receipt_file=$directory/receipt.json ;;
        esac
        receipt=$(read_record "$receipt_file" 2>/dev/null) && validate_receipt "$receipt" || { unverified=true; continue; }
        [[ "$(printf '%s' "$receipt" | json -r .workspace)" == "$WORKSPACE" ]] || { unverified=true; continue; }
        name=$(printf '%s' "$receipt" | json -r .name)
        if [[ "$directory" == "$BACKUPS" ]]; then
            [[ "$receipt_file" == "$BACKUPS/${name%.zip}-receipt.json" ]] || { unverified=true; continue; }
        else
            [[ "$(printf '%s' "$receipt" | json -r .operation)" == "${directory##*/}" ]] || { unverified=true; continue; }
        fi
        validate_zip "$directory/$name" || { unverified=true; continue; }
        [[ "$(file_hash "$directory/$name")" == "$(printf '%s' "$receipt" | json -r .hash)" ]] || { unverified=true; continue; }
        candidates=$(printf '%s' "$candidates" | json --arg directory "$directory" --arg file "$receipt_file" --argjson receipt "$receipt" '.+[{directory:$directory,file:$file,receipt:$receipt}]') || return
    done
    while IFS= read -r encoded; do
        [[ -n "$encoded" ]] || continue
        candidate=$(printf '%s' "$encoded" | json -Rr '@base64d') || return
        receipt=$(printf '%s' "$candidate" | json .receipt)
        directory=$(printf '%s' "$candidate" | json -r .directory)
        receipt_file=$(printf '%s' "$candidate" | json -r .file)
        name=$(printf '%s' "$receipt" | json -r .name)
        [[ "$(read_record "$receipt_file")" == "$receipt" ]] || return 4
        before=$(identity "$directory/$name") || return
        validate_zip "$directory/$name" || return
        [[ "$(file_hash "$directory/$name")" == "$(printf '%s' "$receipt" | json -r .hash)" && "$(identity "$directory/$name")" == "$before" ]] || return 4
        verify_lock && operation_active || return
        /bin/rm "$directory/$name" && /bin/rm "$receipt_file" || return
        if [[ "$directory" != "$BACKUPS" ]]; then
            /bin/rmdir "$directory" || { warning "Extra files preserved in $directory"; return 4; }
        fi
    # Use verified capture times, not filenames or mutable filesystem timestamps.
    # Keep the exact boundary and future timestamps; neither has expired.
    done < <(printf '%s' "$candidates" | json -r --argjson cutoff "$cutoff" '.[] | select((.receipt.captured | fromdateiso8601) < $cutoff) | @base64')
    if $unverified; then warning 'Unverified backup files were preserved; retention covers only verified backups.'; fi
}

# An explicit backup uses the configured cached image, without updating it.
operate_backup() {
    local digest
    select_workspace && load_workspace || return
    inventory_guard && gateway_guard || return
    digest=$(printf '%s' "$IMAGE_CONFIG" | json -r .digest)
    host_readiness "$digest" && acquire_lock && revalidate && prepare_directories && verify_vm_shares || return
    ensure_image "$digest" false && filevault_prompt || return
    create_backup "$IMAGE_CONFIG" || return
    revalidate || return
    retain_backups || warning 'Backup succeeded; cleanup needs manual inspection.'
}
