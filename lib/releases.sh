# Read-only release discovery. No image is run here, and redirects are refused.
validate_http_headers() {
    # Malformed responses must not become an "offline" fallback. Only an
    # otherwise well-formed non-success response is an availability failure.
    /usr/bin/awk '
      /^HTTP\// {blocks++; status=$2; if ($2 !~ /^[0-9][0-9][0-9]$/) bad=1}
      {line=$0; sub(/\r$/, "",line); split(line,a,":"); key=tolower(a[1]);
       if(key=="content-type" || key=="content-length" || key=="link" || key=="location") {if(seen[key]++) bad=1}
       if(key=="location") bad=1}
      END {if(blocks!=1 || bad) exit 4; if(status!=200) exit 3}' "$1"
}
http_get() {
    local url=$1 accept=$2 token=${3:-} config headers status seconds
    case "$url" in
        "$RELEASE_URL"\?*) ;;
        "$REGISTRY_AUTH_URL") ;;
        "$REGISTRY_URL"/*) ;;
        *) return 4 ;;
    esac
    seconds=$((HTTP_DEADLINE-SECONDS)); ((seconds>0)) || return 3
    config=$(/usr/bin/mktemp "$SCRATCH/curl.XXXXXXXX") || return
    headers=$(/usr/bin/mktemp "$SCRATCH/headers.XXXXXXXX") || return
    if [[ -n "$token" ]]; then
        [[ "$token" =~ ^[A-Za-z0-9._~+/-]+=*$ ]] && ((${#token}<=MAX_TOKEN_BYTES)) || return 4
        printf 'header = "Authorization: Bearer %s"\n' "$token" > "$config" || return
    fi
    run_capture "$seconds" "$CURL" -q --config "$config" --silent --show-error --http1.1 --proto '=https' --connect-timeout "$HTTP_CONNECT_TIMEOUT" --max-time "$seconds" --max-filesize "$MAX_JSON_BYTES" --dump-header "$headers" --header "Accept: $accept" "$url"
    status=$?
    /bin/rm "$config" || return
    operation_active || return
    # A size-limit refusal is bad input, not an offline release check. curl can
    # reject Content-Length before downloading, or our capture limit can stop it.
    if ((status==63)) || ! capture_within_limits || (( $(file_stat size "$headers") > MAX_HTTP_HEADER_BYTES )); then
        fail 4 'release response exceeded the allowed size'; return
    fi
    ((status==0)) || return 3
    validate_http_headers "$headers" || return
    HTTP_TYPE=$(/usr/bin/awk 'tolower($0) ~ /^content-type:/ {sub(/^[^:]*:[ \t]*/, ""); sub(/[;\r].*$/, ""); print}' "$headers")
    HTTP_LINK=$(/usr/bin/awk 'tolower($0) ~ /^link:/ {sub(/^[^:]*:[ \t]*/, ""); sub(/\r$/, ""); print}' "$headers")
    HTTP_BODY=$(strict_json < "$CAPTURE_OUT") || return 4
}
release_key() {
    printf '%s' "$1" | json -Rer '
      capture("^v?(?<y>[0-9]{4})\\.(?<m>[0-9]{1,2})\\.(?<d>[0-9]{1,2})(?:\\.(?<p>[0-9]+))?$")
      | map_values(if .==null then 0 else tonumber end)
      | . as $v | (if .y%4==0 and (.y%100!=0 or .y%400==0) then 29 else 28 end) as $feb
      | if .y<1 or .m<1 or .m>12 or .d<1 or .d>([31,$feb,31,30,31,30,31,31,30,31,30,31][.m-1]) or .p>9999 then error("invalid release date") else .y*100000000+.m*1000000+.d*10000+.p end'
}
github_releases() {
    local url="$RELEASE_URL?per_page=$RELEASE_PAGE_SIZE" page=1 all='[]' next tag key
    HTTP_DEADLINE=$((SECONDS+RELEASE_TIMEOUT))
    while :; do
        http_get "$url" application/vnd.github+json || return
        [[ "$HTTP_TYPE" == application/json || "$HTTP_TYPE" == application/vnd.github+json ]] || return 4
        printf '%s' "$HTTP_BODY" | json -e --argjson page_size "$RELEASE_PAGE_SIZE" 'type=="array" and length<=$page_size and all(.[]; (.id|type)=="number" and (.id|floor)==.id and .id>0 and (.draft|type)=="boolean" and (.prerelease|type)=="boolean")' >/dev/null || return 4
        all=$(printf '%s' "$all" | json --argjson page "$HTTP_BODY" '.+$page') || return
        printf '%s' "$all" | json -e '[.[].id] | length == (unique|length)' >/dev/null || return 4
        [[ -n "$HTTP_LINK" ]] || break
        next=$(printf '%s' "$HTTP_LINK" | json -Rer --arg base "$RELEASE_URL?" '
          split(",") | map([capture("^ *<(?<url>[^>]+)> *; *rel=\"(?<rel>next|prev|first|last)\" *$")] | if length==1 then .[0] else error("malformed pagination") end)
          | if all(.[]; .url | startswith($base)) then . else error("foreign pagination URL") end
          | if ([.[].rel]|length!=(unique|length)) then error("duplicate link") else . end
          | map(select(.rel=="next")) | if length==0 then "" elif length!=1 then error("next") else .[0].url end') || return 4
        [[ -n "$next" ]] || break
        page=$((page+1))
        [[ "$next" == "$RELEASE_URL?per_page=$RELEASE_PAGE_SIZE&page=$page" || "$next" == "$RELEASE_URL?page=$page&per_page=$RELEASE_PAGE_SIZE" ]] || return 4
        url=$next
    done
    all=$(printf '%s' "$all" | json '[.[] | select(.draft==false and .prerelease==false)]') || return
    [[ "$all" != '[]' ]] || return 3
    printf '%s' "$all" | json -e '[.[].tag_name] | length == (unique|length)' >/dev/null || return 4
    while IFS= read -r tag; do
        key=$(release_key "$tag") || return 4
        all=$(printf '%s' "$all" | json --arg tag "$tag" --argjson key "$key" 'map(if .tag_name==$tag then .+{sort_key:$key} else . end)') || return
    done < <(printf '%s' "$all" | json -r '.[].tag_name')
    printf '%s' "$all" | json -e 'all(.[]; (.published_at|type)=="string" and (.published_at|test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")) and (.published_at as $date | try (($date | fromdateiso8601 | todateiso8601)==$date) catch false))' >/dev/null || return 4
    printf '%s' "$all" | json 'sort_by(.published_at,.sort_key,.id) | last | {tag:.tag_name,id:.id,published:.published_at}'
}
registry_children() {
    local tag=$1 token=$2
    release_key "$tag" >/dev/null || return 4
    http_get "$REGISTRY_URL/$tag" 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' "$token" || return
    [[ "$HTTP_TYPE" == application/vnd.oci.image.index.v1+json || "$HTTP_TYPE" == application/vnd.docker.distribution.manifest.list.v2+json ]] || return 4
    printf '%s' "$HTTP_BODY" | json -e '
      if .schemaVersion!=2 or (.manifests|type)!="array" then error("index") else . end
      | .manifests as $all
      | if all($all[]; (.digest|test("^sha256:[a-f0-9]{64}$")) and (.size|type)=="number" and (.size|floor)==.size and .size>0 and (.platform|type)=="object") then . else error("descriptor") end
      | if all($all[]; (.mediaType=="application/vnd.oci.image.manifest.v1+json" or .mediaType=="application/vnd.docker.distribution.manifest.v2+json")
          and (if .platform.os=="unknown" or .platform.architecture=="unknown" then
            .platform.os=="unknown" and .platform.architecture=="unknown"
            and .annotations["vnd.docker.reference.type"]=="attestation-manifest"
            and (.annotations["vnd.docker.reference.digest"] as $subject | any($all[]; .digest==$subject and .platform.os=="linux"))
          else (.platform.os=="linux" or .platform.os=="windows")
            and (.platform.architecture as $a | ["arm64","amd64","arm","386","ppc64le","s390x","riscv64"] | index($a)!=null) end)) then . else error("unsupported descriptor") end
      | [$all[] | select(.platform.os=="linux" and (.platform.architecture=="arm64" or .platform.architecture=="amd64"))]
      | if length!=2 or ([.[].platform.architecture]|unique|length)!=2 then error("paired native images required") else . end
      | if all(.[]; (.platform.architecture=="arm64" and (.platform.variant==null or .platform.variant=="v8")) or (.platform.architecture=="amd64" and .platform.variant==null)) then . else error("variant") end
      | map({key:.platform.architecture,value:.digest}) | from_entries' || return 4
}
resolve_online() {
    local current=$1 newest token children current_children tag
    newest=$(github_releases) || return
    HTTP_DEADLINE=$((SECONDS+RELEASE_TIMEOUT))
    http_get "$REGISTRY_AUTH_URL" application/json || return
    [[ "$HTTP_TYPE" == application/json ]] || return 4
    token=$(printf '%s' "$HTTP_BODY" | json -er 'if .token!=null and .access_token!=null and .token!=.access_token then error("ambiguous token") else .token // .access_token end | select(type=="string")') || return 4
    if [[ "$current" != null ]]; then
        tag=$(printf '%s' "$current" | json -r .tag)
        current_children=$(registry_children "$tag" "$token") || return
        [[ "$(printf '%s' "$current_children" | json -r --arg native "$NATIVE" '.[$native]')" == "$(printf '%s' "$current" | json -r .digest)" ]] || { fail 4 'configured registry tag drifted'; return; }
    fi
    tag=$(printf '%s' "$newest" | json -r .tag)
    children=$(registry_children "$tag" "$token") || return
    printf '%s' "$newest" | json --argjson children "$children" --arg native "$NATIVE" '{tag,platform:$native,digest:$children[$native]}'
}
# Return SELECTED and ALLOW_PULL together so fallback remains cache-only.
choose_image() {
    local current=$1 candidate status old_key new_key
    ALLOW_PULL=true
    candidate=$(resolve_online "$current"); status=$?
    operation_active || return
    if ((status!=0)); then
        if ((status==3)) && [[ "$current" != null ]]; then
            require_cached_image "$(printf '%s' "$current" | json -r .digest)" || return
            warning 'Release check unavailable; using the configured cached image.'
            # Cache eviction after this check must not turn offline fallback into a pull.
            ALLOW_PULL=false
            SELECTED=$current; return
        fi
        if ((status==3)); then
            error 'Release check unavailable; first setup/restore needs an online image selection. Try again when GitHub and Docker Hub are reachable.'
        elif ((status==4)); then
            # Some schema checks return only a status. Always explain the stop.
            error 'Release check returned invalid or inconsistent metadata; cannot continue.'
        fi
        return "$status"
    fi
    validate_record image "$candidate" || return 4
    if [[ "$current" == null ]]; then
        confirm "Accept Hermes $(printf '%s' "$candidate" | json -r .tag)?" || return
    elif [[ "$candidate" != "$current" ]]; then
        old_key=$(release_key "$(printf '%s' "$current" | json -r .tag)") && new_key=$(release_key "$(printf '%s' "$candidate" | json -r .tag)") || return
        ((new_key>=old_key)) || { fail 4 'release downgrade refused'; return; }
        printf '1) Continue with Hermes %s\n2) Update to Hermes %s\n' \
            "$(printf '%s' "$current" | json -r .tag)" "$(printf '%s' "$candidate" | json -r .tag)" >&2
        if [[ "$ACTION" == start || "$ACTION" == chat ]]; then
            printf 'Update will stop Hermes if needed, create a verified backup, then restart with the new image.\n' >&2
        fi
        select_number 'Select option' 2 || return
        [[ "$MENU_SELECTION" != 1 ]] || candidate=$current
    fi
    SELECTED=$candidate
}
