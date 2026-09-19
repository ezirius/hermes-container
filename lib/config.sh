# Parse a small NAME=value file without source, eval or shell expansion.
# Keep validation here; keep every default in config/hermes-container.conf.
CONFIG_KEYS='TRUSTED_PATH TRUSTED_TOOL_USERS IMAGE_REPOSITORY RELEASE_REPOSITORY WORKSPACE_BASE
HOME_PATH BACKUPS_PATH AGENT_DOCS_PATH USER_DOCS_PATH DASHBOARD_PORTS
CONTAINER_CPUS CONTAINER_MEMORY CONTAINER_SHM_SIZE RESTART_POLICY
MIN_MACOS_ARM64 MIN_MACOS_AMD64 MIN_PODMAN_ARM64 MIN_PODMAN_MACOS_AMD64
MIN_PODMAN_LINUX_AMD64 MIN_JQ MIN_SERVICE_RELEASE MIN_ENGINE_CPUS
MIN_ENGINE_MEMORY_MIB CACHED_IMAGE_FREE_GIB NEW_IMAGE_FREE_GIB
BACKUP_RESERVE_GIB RESTORE_RESERVE_GIB BACKUP_RETENTION_DAYS
INSPECT_TIMEOUT RELEASE_TIMEOUT HTTP_CONNECT_TIMEOUT PULL_TIMEOUT
MAINTENANCE_TIMEOUT ZIP_LIST_TIMEOUT ZIP_TEST_TIMEOUT SERVICE_START_TIMEOUT
SERVICE_RESTART_TIMEOUT SERVICE_CONTROL_TIMEOUT GATEWAY_STOP_TIMEOUT
CONTAINER_STOP_TIMEOUT CONTAINER_STOP_GRACE DASHBOARD_STOP_TIMEOUT_MS
DASHBOARD_ATTEMPTS DASHBOARD_POLL_SECONDS DASHBOARD_CONNECT_TIMEOUT
DASHBOARD_HTTP_TIMEOUT DASHBOARD_CAPTURE_TIMEOUT DASHBOARD_CONTAINER_PORT
MAX_JSON_BYTES MAX_JSON_DEPTH MAX_STDERR_BYTES MAX_HTTP_HEADER_BYTES
MAX_TOKEN_BYTES MAX_PATH_STEPS MAX_STDOUT_BYTES
IMAGE_REGISTRY RELEASE_API_ORIGIN REGISTRY_API_ORIGIN REGISTRY_AUTH_ORIGIN
REGISTRY_AUTH_SERVICE RELEASE_PAGE_SIZE DEFAULT_TERM
PROCESS_POLL_SECONDS CHILD_TERM_GRACE_SECONDS'

config_value_valid() {
    local key=$1 value=$2 part account port seen=' '
    [[ -n "$value" && "$value" != *[[:cntrl:]]* ]] || return 1
    case "$key" in
        TRUSTED_TOOL_USERS)
            [[ "$value" =~ ^[a-z_][a-z0-9_-]*(\ [a-z_][a-z0-9_-]*)*$ ]] ;;
        TRUSTED_PATH)
            [[ "$value" != :* && "$value" != *: && "$value" != *::* ]] || return 1
            local directories
            IFS=: read -r -a directories <<< "$value"
            for part in "${directories[@]}"; do
                [[ "$part" == /* && "$part" != *[\$\`\"\']* ]] || return 1
                case "/${part#/}/" in */../*|*/./*|*//*) return 1 ;; esac
            done ;;
        IMAGE_REGISTRY|REGISTRY_AUTH_SERVICE)
            config_server_valid "$value" ;;
        RELEASE_API_ORIGIN|REGISTRY_API_ORIGIN|REGISTRY_AUTH_ORIGIN)
            [[ "$value" == https://* ]] && config_server_valid "${value#https://}" ;;
        RELEASE_PAGE_SIZE) [[ "$value" =~ ^[1-9][0-9]{0,2}$ ]] && ((value<=100)) ;;
        DEFAULT_TERM) [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9+._-]*$ ]] ;;
        PROCESS_POLL_SECONDS|CHILD_TERM_GRACE_SECONDS)
            [[ "$value" =~ ^[0-9]{1,3}(\.[0-9]{1,3})?$ && "$value" =~ [1-9] ]] ;;
        IMAGE_REPOSITORY)
            [[ "$value" =~ ^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9._-]*$ ]] ;;
        RELEASE_REPOSITORY)
            [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9._-]+$ ]] &&
                [[ "${value#*/}" != . && "${value#*/}" != .. ]] ;;
        WORKSPACE_BASE)
            [[ "$value" == /* && "$value" != / && "$value" != */ ]] || return 1
            case "$value/" in */../*|*/./*|*//*) return 1 ;; esac ;;
        HOME_PATH|BACKUPS_PATH|AGENT_DOCS_PATH|USER_DOCS_PATH)
            [[ "$value" != /* && "$value" != */ ]] || return 1
            case "/$value/" in */../*|*/./*|*//*) return 1 ;; esac
            part=${value//\{workspace\}/Workspace}
            [[ "$part" != *[\{\}]* ]] ;;
        DASHBOARD_PORTS)
            local entries
            read -r -a entries <<< "$value"
            for part in "${entries[@]}"; do
                [[ "$part" =~ ^([a-z][a-z0-9_-]{0,31}):([1-9][0-9]{0,4})$ ]] || return 1
                account=${BASH_REMATCH[1]}; port=${BASH_REMATCH[2]}
                ((port<=65535)) && [[ "$seen" != *" $account "* ]] || return 1
                seen="$seen$account "
            done
            [[ "$seen" == *' default '* ]] ;;
        MAX_*_BYTES) [[ "$value" =~ ^[1-9][0-9]{0,8}$ ]] ;;
        DASHBOARD_CONTAINER_PORT) [[ "$value" =~ ^[1-9][0-9]{0,4}$ ]] && ((value<=65535)) ;;
        CONTAINER_MEMORY|CONTAINER_SHM_SIZE) [[ "$value" =~ ^[1-9][0-9]{0,5}[mg]$ ]] ;;
        RESTART_POLICY) [[ "$value" == unless-stopped || "$value" == always || "$value" == no || "$value" == on-failure ]] ;;
        MIN_PODMAN_*|MIN_JQ) version_at_least "$value" "$value" ;;
        MIN_SERVICE_RELEASE)
            [[ "$value" =~ ^v([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})(\.[0-9]{1,4})?$ ]] || return 1
            local year=$((10#${BASH_REMATCH[1]})) month=$((10#${BASH_REMATCH[2]})) day=$((10#${BASH_REMATCH[3]}))
            local days=(0 31 28 31 30 31 30 31 31 30 31 30 31)
            ((year%4!=0 || (year%100==0 && year%400!=0))) || days[2]=29
            ((year>0 && month>=1 && month<=12 && day>=1 && day<=days[month])) ;;
        *) [[ "$value" =~ ^[1-9][0-9]{0,5}$ ]] ;;
    esac
}
# A service endpoint is a DNS name (or IPv4 address) and an optional TCP port.
# Reject URL paths, credentials, query strings and shell punctuation here.
config_server_valid() {
    local server=$1 host port label labels
    host=${server%%:*}
    [[ "$host" =~ ^[A-Za-z0-9.-]+$ && "$host" != .* && "$host" != *. && "$host" != *..* ]] || return 1
    IFS=. read -r -a labels <<< "$host"
    for label in "${labels[@]}"; do
        [[ "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] && ((${#label}<=63)) || return 1
    done
    if [[ "$server" == *:* ]]; then
        port=${server#*:}
        [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && ((port<=65535)) || return 1
    fi
}
load_launcher_config() {
    local file=$1 contents line key value expected known seen=' ' number=0
    [[ -f "$file" && ! -L "$file" && -r "$file" ]] || {
        fail 3 "launcher configuration is missing, unreadable or symlinked: $file"; return
    }
    # Read one snapshot. A NUL byte is binary input, not an invisible comment.
    if IFS= read -r -d '' contents < "$file"; then
        fail 3 "configuration must be plain text (NUL byte found): $file"; return
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        number=$((number+1))
        case "$line" in ''|\#*) continue ;; esac
        [[ "$line" == *=* ]] || { fail 3 "expected NAME=value at $file:$number"; return; }
        key=${line%%=*}; value=${line#*=}; known=false
        for expected in $CONFIG_KEYS; do
            [[ "$key" != "$expected" ]] || { known=true; break; }
        done
        $known || { fail 3 "unknown setting at $file:$number: $key"; return; }
        [[ "$seen" != *" $key "* ]] || { fail 3 "duplicate setting at $file:$number: $key"; return; }
        config_value_valid "$key" "$value" || { fail 3 "invalid value for $key at $file:$number"; return; }
        printf -v "$key" '%s' "$value"
        seen="$seen$key "
    done <<< "$contents"
    for key in $CONFIG_KEYS; do
        [[ "$seen" == *" $key "* ]] || { fail 3 "missing setting in $file: $key"; return; }
    done
    ((CONTAINER_STOP_TIMEOUT>CONTAINER_STOP_GRACE)) || {
        fail 3 'CONTAINER_STOP_TIMEOUT must exceed CONTAINER_STOP_GRACE'; return
    }
    ((SERVICE_CONTROL_TIMEOUT*1000>DASHBOARD_STOP_TIMEOUT_MS)) || {
        fail 3 'SERVICE_CONTROL_TIMEOUT must exceed DASHBOARD_STOP_TIMEOUT_MS after converting seconds to milliseconds'; return
    }
    ((DASHBOARD_CAPTURE_TIMEOUT>DASHBOARD_HTTP_TIMEOUT)) || {
        fail 3 'DASHBOARD_CAPTURE_TIMEOUT must exceed DASHBOARD_HTTP_TIMEOUT'; return
    }
    ((DASHBOARD_HTTP_TIMEOUT>=DASHBOARD_CONNECT_TIMEOUT)) || {
        fail 3 'DASHBOARD_HTTP_TIMEOUT must be at least DASHBOARD_CONNECT_TIMEOUT'; return
    }
    IMAGE=$IMAGE_REGISTRY/$IMAGE_REPOSITORY
    RELEASE_URL=$RELEASE_API_ORIGIN/repos/$RELEASE_REPOSITORY/releases
    REGISTRY_URL=$REGISTRY_API_ORIGIN/v2/$IMAGE_REPOSITORY/manifests
    REGISTRY_AUTH_URL="$REGISTRY_AUTH_ORIGIN/token?service=$REGISTRY_AUTH_SERVICE&scope=repository:$IMAGE_REPOSITORY:pull"
}

# Convert the supported m/g units to bytes for Podman inspect comparisons.
config_bytes() {
    local value=$1 number=${1%?}
    case "$value" in
        *m) printf '%s\n' "$((number*1048576))" ;;
        *g) printf '%s\n' "$((number*1073741824))" ;;
        *) return 3 ;;
    esac
}
