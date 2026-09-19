# Host differences stay here so the lifecycle code reads the same on both OSes.
HOST_OS=$(/usr/bin/uname -s)
platform_rules() {
    local os=$1 arch=$2 version=${3:-} minimum_macos=$MIN_MACOS_AMD64 major
    case "$os:$arch" in
        Darwin:arm64) NATIVE=arm64; REQUIRED_PODMAN=$MIN_PODMAN_ARM64; minimum_macos=$MIN_MACOS_ARM64 ;;
        Darwin:x86_64) NATIVE=amd64; REQUIRED_PODMAN=$MIN_PODMAN_MACOS_AMD64 ;;
        Linux:x86_64) NATIVE=amd64; REQUIRED_PODMAN=$MIN_PODMAN_LINUX_AMD64 ;;
        *) fail 3 'supported hosts: macOS Apple Silicon/Intel, Linux Intel/AMD x86-64'; return ;;
    esac
    if [[ "$os" == Darwin ]]; then
        # sw_vers supplies the actual OS version. These floors are project policy.
        [[ "$version" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){0,2}$ ]] || {
            fail 3 'unrecognised macOS version'; return
        }
        major=${version%%.*}
        (( 10#$major >= minimum_macos )) || {
            fail 3 "macOS $minimum_macos or newer is required for $arch"; return
        }
    fi
}
file_stat() {
    local field=$1; shift
    if [[ "$HOST_OS" == Linux ]]; then
        case "$field" in identity) field='%d:%i' ;; owner) field='%u' ;; mode) field='%a' ;; size) field='%s' ;; *) return 3 ;; esac
        /usr/bin/stat -c "$field" -- "$@"
    else
        case "$field" in identity) field='%d:%i' ;; owner) field='%u' ;; mode) field='%Lp' ;; size) field='%z' ;; *) return 3 ;; esac
        /usr/bin/stat -f "$field" "$@"
    fi
}
account_home() {
    if [[ "$HOST_OS" == Linux ]]; then
        /usr/bin/getent passwd "$OPERATOR_UID" | /usr/bin/awk -F: 'NF==7 {print $6}'
    else
        /usr/bin/dscacheutil -q user -a uid "$OPERATOR_UID" | /usr/bin/awk '/^dir: / {sub(/^dir: /, ""); print}'
    fi
}
child_environment() {
    CHILD_ENV=(HOME="$ACCOUNT_HOME" PATH="$TRUSTED_PATH" LANG=C LC_ALL=C)
    PODMAN_OPTIONS=()
    if [[ "$HOST_OS" == Linux ]]; then
        # Use this account's local engine, irrespective of remote CLI defaults.
        PODMAN_OPTIONS=(--remote=false)
        RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$OPERATOR_UID}
        [[ "$RUNTIME_DIR" == /* ]] || { fail 3 "XDG_RUNTIME_DIR must be absolute"; return; }
        safe_path "$RUNTIME_DIR" directory 700 || { fail 3 "a private runtime directory is required: $RUNTIME_DIR"; return; }
        CHILD_ENV+=(XDG_RUNTIME_DIR="$RUNTIME_DIR")
        [[ ! -S "$RUNTIME_DIR/bus" ]] || CHILD_ENV+=(DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME_DIR/bus")
    fi
}
