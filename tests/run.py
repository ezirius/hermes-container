#!/usr/bin/env python3
"""Development-only tests. Production never invokes host Python or these fakes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shlex
import shutil
import signal
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TEST_JQ = os.environ.get("HERMES_TEST_JQ") or shutil.which("jq") or "/usr/bin/jq"
# Direct parser tests use the same limits as the launcher configuration.
TEST_SETTINGS = dict(line.split('=', 1) for line in (ROOT/'config/hermes-container.conf').read_text().splitlines()
                     if line and not line.startswith('#'))
JSON_PARSER = [TEST_JQ, '-RscS', '--argjson', 'max_bytes', TEST_SETTINGS['MAX_JSON_BYTES'],
               '--argjson', 'max_depth', TEST_SETTINGS['MAX_JSON_DEPTH'], '-f', str(ROOT/'lib/json.jq')]
BOOT = r'''
source "$1/hermes-container.sh"
load_launcher_config "$CODE_DIR/config/hermes-container.conf" || exit
umask 077
JQ=__TEST_JQ__
SCRATCH=$2/scratch
mkdir -m 700 "$SCRATCH" "$2/Workspace"
OPERATOR_UID=$(id -u); OPERATOR_GID=$(id -g)
RUNTIME_BASE=$2/runtime
mkdir -m 700 "$RUNTIME_BASE"
unset XDG_CONFIG_HOME XDG_RUNTIME_DIR
ACCOUNT_HOME=$2
ACCOUNT_USER=workspace
CHILD_ENV=(HOME="$ACCOUNT_HOME" PATH="$TRUSTED_PATH" LANG=C LC_ALL=C)
PODMAN_OPTIONS=()
ACTION=setup; WORKSPACE_ARGUMENT=Workspace; NATIVE=arm64; APPLE=
CAPTURE_NUMBER=0; CHILD_PID=; INTERRUPTED=
LOCK_HELD=false; RESIDUAL_UNKNOWN=false
OPERATION_ID=0123456789abcdef0123456789abcdef
set_paths "$2/Workspace" || exit
# Existing source parents keep older focused tests independent of parent creation.
mkdir -p "$WORKSPACE/Apps Data/Hermes" "$USER_HOME/Documents/$WORKSPACE_NAME/Apps Data/Hermes"
WORKSPACE_INODE=$(identity "$WORKSPACE")
'''
BOOT = BOOT.replace('__TEST_JQ__', shlex.quote(TEST_JQ))
LEGACY_IMAGE = {'tag': 'v2026.7.20', 'id': 1, 'published': '2026-07-20T00:00:00Z',
         'arm64': 'sha256:'+'a'*64, 'amd64': 'sha256:'+'b'*64,
         'platform': 'arm64', 'digest': 'sha256:'+'a'*64}

IMAGE = {key: LEGACY_IMAGE[key] for key in ('tag', 'platform', 'digest')}

class LauncherTests(unittest.TestCase):
    def shell(self, code, expect=0, setup=None, stdin=None):
        with tempfile.TemporaryDirectory(prefix='hermes-bash-test-') as value:
            # macOS /var is an alias; selection requires a canonical host path.
            value = str(Path(value).resolve())
            os.chmod(value, 0o700)
            if setup:
                setup(Path(value))
            result = subprocess.run(['/bin/bash', '-s', '--', str(ROOT), value],
                                    input=BOOT+'\n'+code, text=True, capture_output=True,
                                    timeout=30)
            self.assertEqual(result.returncode, expect, result.stdout+'\n'+result.stderr)
            return result

    def test_missing_or_wrong_type_path_reports_the_path(self):
        for target in ('$ACCOUNT_HOME/missing', '$ACCOUNT_HOME'):
            result = self.shell(f'safe_path "{target}" file', expect=4)
            self.assertIn('required file is missing, inaccessible or the wrong type:', result.stderr)
            self.assertIn('/hermes-bash-test-', result.stderr)

    def test_tool_discovery_reports_missing_or_rejected_tool(self):
        result = self.shell('find_tool hermes-test-nonexistent-tool', expect=3)
        self.assertIn('hermes-test-nonexistent-tool is unavailable', result.stderr)
        # Simulate foreign ownership without changing any installed executable.
        result = self.shell(r'''
file_stat() {
    case "$1" in owner) printf '999999\n' ;; mode) printf '755\n' ;; esac
}
find_tool sh
''', expect=3)
        self.assertIn('sh is unavailable', result.stderr)
        self.assertIn('owned by root or this user', result.stderr)

    def test_tool_discovery_accepts_root_or_current_user(self):
        self.shell(r'''
for tool_owner in 0 "$OPERATOR_UID"; do
    file_stat() {
        case "$1" in owner) printf '%s\n' "$tool_owner" ;; mode) printf '755\n' ;; esac
    }
    found=$(find_tool sh) || exit
    [[ -x "$found" ]] || exit 1
done
''')

    def test_tool_discovery_accepts_configured_owner_but_rejects_writable_tools(self):
        self.shell(r'''
tool_owner=$OPERATOR_UID
TRUSTED_TOOL_USERS=$(/usr/bin/id -un "$tool_owner")
OPERATOR_UID=999998
file_stat() {
    case "$1" in owner) printf '%s\n' "$tool_owner" ;; mode) printf '%s\n' "$tool_mode" ;; esac
}
tool_mode=555
found=$(find_tool sh) || exit
[[ -x "$found" ]] || exit 1
for tool_mode in 775 757 777; do
    if find_tool sh; then exit 1; fi
done
tool_mode=555
TRUSTED_TOOL_USERS=untrusted_fixture_account
if find_tool sh; then exit 1; fi
''')

    def test_file_hash_ignores_filename_escaping(self):
        def setup(root):
            for name in ('ordinary', 'with spaces', 'with\\backslash'):
                (root/name).write_bytes(b'fixture')
        result = self.shell(r'''
for name in ordinary 'with spaces' 'with\backslash'; do
 file_hash "$ACCOUNT_HOME/$name" || exit
done
if file_hash "$ACCOUNT_HOME/missing"; then exit 1; fi
''', setup=setup)
        self.assertEqual(result.stdout.splitlines(),
                         [hashlib.sha256(b'fixture').hexdigest()] * 3)

    def test_requested_nala_paths_preserve_case(self):
        self.shell(r'''
# Both paths follow the account: lowercase login, initial-capital workspace.
for name in Nala Ezirius Mckay; do
 ACCOUNT_USER=$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')
 for home_base in /Users /home; do
  ACCOUNT_HOME=$home_base/$ACCOUNT_USER
  set_paths "/Volumes/Data/$name" || exit
  [[ "$HOME_SOURCE" == "/Volumes/Data/$name/Apps Data/Hermes/Home" ]] || exit 1
  [[ "$BACKUPS_SOURCE" == "/Volumes/Data/$name/Apps Data/Hermes/Backups" ]] || exit 1
  [[ "$BACKUPS" == "$(canonical_path "$BACKUPS_SOURCE")" ]] || exit 1
  [[ "$DOCS_SOURCE" == "/Volumes/Data/$name/Apps Data/Hermes/Agent Docs" ]] || exit 1
  [[ "$USER_DOCS_SOURCE" == "$home_base/$ACCOUNT_USER/Documents/$name/Apps Data/Hermes/User Docs" ]] || exit 1
  [[ "$CONTAINER_DOCS" == "$DOCS_SOURCE" && "$CONTAINER_USER_DOCS" == "$USER_DOCS_SOURCE" ]] || exit 1
 done
done
ACCOUNT_USER=mckay
for name in mckay McKay MCKAY; do
 if set_paths "/Volumes/Data/$name"; then exit 1; fi
done
ACCOUNT_USER=ezirius
if set_paths /Volumes/Data/Nala; then exit 1; fi
''')


    def test_root_owned_volume_with_user_owned_workspace(self):
        self.shell(r'''
workspace_base() { printf '%s/volume\n' "$ACCOUNT_HOME"; }
mkdir -m 700 "$ACCOUNT_HOME/volume" "$ACCOUNT_HOME/volume/Nala"
ACCOUNT_USER=nala; WORKSPACE_ARGUMENT=Nala
# Model root ownership without changing any real directory ownership.
base_owner=0; base_mode=755
file_stat() {
 if [[ "$2" == "$ACCOUNT_HOME/volume" ]]; then
  case "$1" in owner) printf '%s\n' "$base_owner"; return ;; mode) printf '%s\n' "$base_mode"; return ;; esac
 fi
 # Keep real filesystem checks for the user-owned workspace and its sources.
 if [[ "$HOST_OS" == Linux ]]; then
  case "$1" in owner) stat -c %u "$2" ;; mode) stat -c %a "$2" ;; identity) stat -c %d:%i "$2" ;; esac
 else
  case "$1" in owner) stat -f %u "$2" ;; mode) stat -f %Lp "$2" ;; identity) stat -f %d:%i "$2" ;; esac
 fi
}
select_workspace || exit
[[ "$WORKSPACE_NAME" == Nala ]] || exit 1
base_mode=777
if select_workspace; then exit 1; fi
base_mode=755; base_owner=$((OPERATOR_UID+1))
if select_workspace; then exit 1; fi
''')

    def test_four_mounts_and_backup_exclusions(self):
        self.shell(r'''
image='IMAGE_PLACEHOLDER'
build_runtime "$image" chat "$HERMES_DATA" || exit
printf '%s\n' "${RUNTIME[@]}" > "$SCRATCH/args"
[[ $(grep -c '^--mount$' "$SCRATCH/args") == 4 ]] || exit 1
grep -Fx -- "$(mount_value "$BACKUPS" /opt/data/backups)" "$SCRATCH/args" || exit
grep -Fx -- "$(mount_value "$HERMES_DATA" /opt/data)" "$SCRATCH/args" || exit
grep -Fx -- "$(mount_value "$DOCUMENTS" "$CONTAINER_DOCS")" "$SCRATCH/args" || exit
grep -Fx -- "$(mount_value "$USER_DOCUMENTS" "$CONTAINER_USER_DOCS")" "$SCRATCH/args" || exit
grep -Fx -- '--workdir=/opt/data' "$SCRATCH/args" || exit
grep -Fx -- "TERMINAL_CWD=$CONTAINER_DOCS" "$SCRATCH/args" || exit
build_runtime "$image" backup "$HERMES_DATA" "$BACKUPS/.pending-test/capture-1" || exit
printf '%s\n' "${RUNTIME[@]}" > "$SCRATCH/args"
[[ $(grep -c '^--mount$' "$SCRATCH/args") == 2 ]] || exit 1
grep -Fx -- "$(mount_value "$BACKUPS" /opt/data/backups)" "$SCRATCH/args" || exit
grep -Fx -- "/opt/data/backups/.pending-test/capture-1" "$SCRATCH/args" || exit
if grep -F 'target=/Volumes/Data/' "$SCRATCH/args"; then exit 1; fi
if grep -F 'target=/Users/' "$SCRATCH/args"; then exit 1; fi
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_user_docs_conflict_and_parent_replacement(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
inventory_snapshot() {
 json -n --arg path "$USER_DOCUMENTS" '[{id:("0"*64),state:"running",labels:{},mounts:[{source:$path,target:"/data"}]}]'
}
inventory_guard; [[ $? == 4 ]] || exit 1
parent=${USER_DOCUMENTS%/*}
mv "$parent" "$parent.saved"
mkdir -m 700 "$parent" "$USER_DOCUMENTS"
if verify_lock; then exit 1; fi
''')

    def test_mount_parent_retarget_is_refused_before_metadata_creation(self):
        self.shell(r'''
parent="$WORKSPACE/Apps Data"
mv "$parent" "$WORKSPACE/saved"
ln -s "$WORKSPACE/saved" "$parent"
if load_workspace; then exit 1; fi
[[ ! -e "$METADATA" ]]
''')

    def test_linked_home_docs_and_parents_use_real_host_paths(self):
        self.shell(r'''
mkdir -m 700 "$ACCOUNT_HOME/real-home" "$ACCOUNT_HOME/real-docs"
ln -s "$ACCOUNT_HOME/real-home" "$HERMES_DATA"
ln -s "$ACCOUNT_HOME/real-docs" "$DOCUMENTS"
parent="$USER_HOME/Documents/$WORKSPACE_NAME/Apps Data/Hermes"
mv "$parent" "$ACCOUNT_HOME/user-parent"
ln -s "$ACCOUNT_HOME/user-parent" "$parent"
set_paths "$WORKSPACE" || exit
load_workspace && acquire_lock && prepare_directories || exit
[[ $HERMES_DATA == "$ACCOUNT_HOME/real-home" && $DOCUMENTS == "$ACCOUNT_HOME/real-docs" ]] || exit 1
[[ $CONTAINER_DOCS == "$WORKSPACE/Apps Data/Hermes/Agent Docs" ]] || exit 1
[[ $BACKUPS == "$WORKSPACE/Apps Data/Hermes/Backups" ]] || exit 1
verify_lock || exit
mkdir "$ACCOUNT_HOME/replacement"
rm "$HOME_SOURCE"
ln -s "$ACCOUNT_HOME/replacement" "$HOME_SOURCE"
if verify_lock; then exit 1; fi
''')

    def test_broken_or_unsafe_home_link_is_refused(self):
        for unsafe in (False, True):
            self.shell(r'''
target=$ACCOUNT_HOME/target
if UNSAFE; then mkdir -m 777 "$target"; fi
ln -s "$target" "$HERMES_DATA"
set_paths "$WORKSPACE" || exit
if load_workspace; then exit 1; fi
[[ ! -e "$CONFIG_DIR" ]]
'''.replace('UNSAFE', 'true' if unsafe else 'false'))

    def test_parent_symlink_cycle_returns_without_hanging(self):
        with tempfile.TemporaryDirectory(prefix='hermes-link-cycle-') as value:
            root = Path(value)
            (root/'a').symlink_to('b/child')
            (root/'b').symlink_to('a')
            process = subprocess.Popen(
                ['/bin/bash', '-c',
                 'source "$1/hermes-container.sh"; load_launcher_config "$CODE_DIR/config/hermes-container.conf" || exit; canonical_path "$2"',
                 'check', str(ROOT), str(root/'a')],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # Stop only this isolated test group, including recursive shells.
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                self.fail('parent symlink cycle did not terminate')
            self.assertEqual(process.returncode, 4, stderr)
            self.assertIn(b'path resolution exceeded 64 steps', stderr)
            self.assertEqual(stdout, b'')

    def test_linked_docs_cannot_overlap_private_home(self):
        # Check the same directory, a child of Home and a parent containing Home.
        for docs in ('DOCUMENTS', 'USER_DOCUMENTS'):
            for destination in ('"$HERMES_DATA"', '"$HERMES_DATA/notes"',
                                '"${HERMES_DATA%/*}"'):
                with self.subTest(docs=docs, destination=destination):
                    self.shell(r'''
mkdir -m 700 "$HERMES_DATA" "$HERMES_DATA/notes"
ln -s DESTINATION "$DOCS_VARIABLE"
set_paths "$WORKSPACE" || exit
if verify_source_paths; then exit 1; fi
[[ ! -e "$CONFIG_DIR" && ! -e "$ACTIVE_LOCK" ]]
'''.replace('DESTINATION', destination).replace('DOCS_VARIABLE', docs))

    def test_source_identity_cannot_be_refreshed_after_replacement(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
before=$(printf '%s\n' "${SOURCE_PIN_IDS[@]}")
mv "$USER_DOCUMENTS" "$USER_DOCUMENTS.saved"
mkdir -m 700 "$USER_DOCUMENTS"
if pin_source_paths; then exit 1; fi
if pin_source_path "$USER_DOCUMENTS"; then exit 1; fi
[[ $(printf '%s\n' "${SOURCE_PIN_IDS[@]}") == "$before" ]] || exit 1
if verify_lock; then exit 1; fi
''')

    def test_cancelled_capture_does_not_dispatch(self):
        self.shell(r'''
INTERRUPTED=2
run_capture 1 /usr/bin/touch "$SCRATCH/should-not-exist"
status=$?
[[ $status == 130 && ! -e "$SCRATCH/should-not-exist" ]]
''')

    def test_cancelled_json_capture_keeps_signal_status(self):
        self.shell(r'''
INTERRUPTED=15
capture_json /usr/bin/true
[[ $? == 143 ]]
''')

    def test_main_keeps_cancellation_over_helper_error(self):
        self.shell(r'''
initialise() { return 0; }
operate() { INTERRUPTED=2; return 3; }
main setup workspace
[[ $? == 130 ]]
''')

    def test_capture_cancellation_reaps_child_without_waiting_for_timeout(self):
        self.shell(r'''
trap 'interrupted 15' TERM
(sleep 0.2; kill -TERM $$) &
started=$SECONDS
run_capture 20 /bin/bash -c 'trap "" TERM; echo $$ > "$1"; exec /bin/sleep 20' sh "$SCRATCH/child.pid"
status=$?
[[ $status == 143 && -z "$CHILD_PID" ]] || exit 1
((SECONDS-started<10)) || exit 1
! kill -0 "$(cat "$SCRATCH/child.pid")" 2>/dev/null
''')


    def test_interactive_cancellation_reaps_stubborn_local_child(self):
        for command in ('run_session unused setup', 'service_chat'):
            with self.subTest(command=command):
                self.shell(r'''
verify_lock() { return 0; }
gateway_guard() { return 0; }
inventory_guard() { return 0; }
build_runtime() { RUNTIME=(); }
PODMAN=$SCRATCH/podman
cat > "$PODMAN" <<'CHILD'
#!/bin/bash
trap '' TERM
printf '%s\n' "$$" > "$HOME/child.pid"
exec /bin/sleep 20
CHILD
chmod 700 "$PODMAN"
trap 'interrupted 15' TERM
# Wait for the fake client to install its handler before cancelling it.
(while [[ ! -f "$ACCOUNT_HOME/child.pid" ]]; do sleep 0.05; done; kill -TERM $$) &
notifier=$!
started=$SECONDS
COMMAND_PLACEHOLDER
status=$?
wait "$notifier"
[[ $status == 143 && -z "$CHILD_PID" ]] || exit 1
((SECONDS-started<10)) || exit 1
! kill -0 "$(cat "$ACCOUNT_HOME/child.pid")" 2>/dev/null
'''.replace('COMMAND_PLACEHOLDER', command))

    def test_machine_connection_is_verified_and_fixed(self):
        self.shell(r'''
port=1234
machine_name=test
rootful=false
machine_state=running
podman_json() {
 case "$1 $2" in
 'machine info') printf '{"Host":{"DefaultMachine":"test"}}' ;;
 'machine inspect') json -n --arg name "$machine_name" --arg state "$machine_state" --argjson rootful "$rootful" '[{Name:$name,State:$state,Rootful:$rootful,SSHConfig:{RemoteUsername:"core",Port:1234,IdentityPath:"/tmp/key"}}]' ;;
 'system connection') json -n --arg uri "ssh://core@127.0.0.1:$port/run/user/501/podman/podman.sock" '[{Default:true,IsMachine:true,URI:$uri,Identity:"/tmp/key"}]' ;;
 *) return 99 ;;
 esac
}
bind_machine_connection || exit
[[ ${PODMAN_OPTIONS[0]} == --url && ${PODMAN_OPTIONS[3]} == /tmp/key ]] || exit
port=9999
if bind_machine_connection; then exit 1; fi
port=1234
machine_name=other
if bind_machine_connection; then exit 1; fi
machine_name=test
rootful=true
if bind_machine_connection; then exit 1; fi
rootful=false
machine_state=stopped
if bind_machine_connection; then exit 1; fi
''')

    def test_session_cancellation_keeps_status_and_residual_marker(self):
        self.shell(r'''
verify_lock() { return 0; }
gateway_guard() { return 0; }
inventory_guard() {
 [[ -z "$INTERRUPTED" ]] || return 99
}
build_runtime() { RUNTIME=(20); }
PODMAN=/bin/sleep
trap 'interrupted 15' TERM
(sleep 0.2; kill -TERM $$) &
run_session '{}' chat
status=$?
[[ $status == 143 && $RESIDUAL_UNKNOWN == true && -z "$CHILD_PID" ]]
''')

    def test_minimum_version_comparison(self):
        self.shell(r'''
version_at_least 6.1.1 6.1.1 || exit
version_at_least 6.10.0 6.9.9 || exit
version_at_least 7.0 6.1.1 || exit
version_at_least 1.7 1.7.0 || exit
if version_at_least 6.1.0 6.1.1; then exit 1; fi
if version_at_least 6.1.2-rc1 6.1.1; then exit 1; fi
if version_at_least invalid 6.1.1; then exit 1; fi
''')

    def test_jq_version_check(self):
        self.shell(r'''
check_jq || exit
JQ=fake_jq
fake_jq() { printf '%s\n' "$fake_version"; }
for fake_version in jq-1.7 jq-1.7.1-apple jq-1.8.2 jq-1.70 jq-2.0; do
    check_jq || exit
done
for fake_version in jq-1.6 jq-1.8.2garbage jq-1.8.0-rc1; do
    if check_jq; then exit 1; fi
done
fake_jq() { return 1; }
if check_jq; then exit 1; fi
''')

    def test_shell_syntax(self):
        for path in [ROOT/'hermes-container.sh', *sorted((ROOT/'lib').glob('*.sh'))]:
            subprocess.run(['/bin/bash', '-n', str(path)], check=True)

    def test_offline_cli(self):
        for arg in ['--help', '-h']:
            result = subprocess.run([str(ROOT/'hermes-container.sh'), arg], capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, b'')
        for args in [['help'], ['unknown'], ['--version'], ['-v'],
                     ['setup', ''], ['chat', '../x'],
                     ['chat', 'x', 'y'], ['chat', '--unknown'], ['backup', '-h'],
                     ['dashboard'], ['dashboard', 'ezirius'], ['start', '--unknown'], ['start', 'ezirius', 'extra'],
                     ['restore', '--unknown'], ['import'], ['setup', 'two words'],
                     ['chat', 'x' * 33]]:
            result = subprocess.run([str(ROOT/'hermes-container.sh'), *args], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b'')
            self.assertTrue(result.stderr.startswith(
                b'Error: invalid arguments. Use one of the forms below.\n\nUsage:'))
            self.assertIn(b'Usage: hermes-container.sh', result.stderr)

    def test_json_valid(self):
        value = {'a': {'x': 1}, 'b': [True, None, 'quotes " and \\ and λ']}
        result = subprocess.run(JSON_PARSER,
                                input=json.dumps(value), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), value)

    def test_json_invalid(self):
        cases = ['{"a":1,"a":2}', '{"a":{"b":1},"a":{"c":2}}',
                 '{"a":1,"\\u0061":2}', '{} {}', '[1,]', '{"a":1,}',
                 'NaN', '1e999', '9007199254740992', '"bad\\q"',
                 '['*66+'0'+']'*66, '{bad}', 'truex']
        for value in cases:
            with self.subTest(value=value):
                result = subprocess.run(JSON_PARSER,
                                        input=value, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_record_private_and_canonical(self):
        self.shell(r'''
printf '{"a":1,"a":2}\n' > "$SCRATCH/state"
chmod 600 "$SCRATCH/state"
if read_record "$SCRATCH/state"; then exit 1; fi
printf '{"a":1}\n' > "$SCRATCH/state"
read_record "$SCRATCH/state" || exit
chmod 644 "$SCRATCH/state"
if read_record "$SCRATCH/state"; then exit 1; fi
''')


    def test_python_layout_is_preserved(self):
        self.shell(r'''
mkdir "$METADATA"
printf 'sentinel' > "$METADATA/foreign"
if load_workspace; then exit 1; fi
[[ $(cat "$METADATA/foreign") == sentinel ]] || exit 1
''')


    def test_replaced_backups_directory_refused(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
mv "$BACKUPS" "$HERMES_DATA/saved-backups"
mkdir -m 700 "$BACKUPS"
if verify_lock; then exit 1; fi
''')

    def test_backup_capacity_excludes_existing_archives(self):
        def setup(root):
            with (root/'old.zip').open('wb') as archive:
                archive.truncate(2 * 1024**3)
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
mv "$ACCOUNT_HOME/old.zip" "$BACKUPS/old.zip"
free_bytes() { printf '1100000000\n'; }
check_backup_sources || exit
if grep -F "$BACKUPS/old.zip" "$SCRATCH/source-names"; then exit 1; fi
''', setup=setup)

    def test_nonempty_home_refused(self):
        result = self.shell(r'''
mkdir "$HERMES_DATA"; printf existing > "$HERMES_DATA/data"
if load_workspace; then exit 1; fi
[[ ! -e "$METADATA" && $(cat "$HERMES_DATA/data") == existing ]] || exit 1
''')
        self.assertIn('existing entry:', result.stderr)
        self.assertIn('/Home/data"', result.stderr)

    def test_first_setup_and_import_refuse_old_backups_without_deleting_them(self):
        for action in ('setup', 'restore'):
            with self.subTest(action=action):
                result = self.shell(r'''
ACTION=ACTION_PLACEHOLDER
mkdir -m 700 "$HERMES_DATA" "$HERMES_DATA/backups"
printf preserve > "$HERMES_DATA/backups/old.zip"
load_workspace; [[ $? == 4 ]] || exit 1
[[ $(cat "$HERMES_DATA/backups/old.zip") == preserve ]] || exit 1
[[ ! -e "$CONFIG_DIR" && ! -e "$ACTIVE_LOCK" ]]
'''.replace('ACTION_PLACEHOLDER', action))
                self.assertIn('inspect or migrate it manually', result.stderr)

    def test_first_setup_refuses_any_old_backups_path(self):
        for kind in ('empty', 'file', 'symlink', 'broken', 'writable'):
            with self.subTest(kind=kind):
                self.shell(r'''
mkdir -m 700 "$HERMES_DATA"
case KIND in
 empty) mkdir -m 700 "$HERMES_DATA/backups" ;;
 broken) ln -s "$SCRATCH/missing" "$HERMES_DATA/backups" ;;
 file) printf preserve > "$HERMES_DATA/backups" ;;
 symlink) mkdir "$SCRATCH/old-backups"; ln -s "$SCRATCH/old-backups" "$HERMES_DATA/backups" ;;
 writable) mkdir -m 777 "$HERMES_DATA/backups" ;;
esac
load_workspace; [[ $? == 4 ]] || exit 1
[[ ! -e "$CONFIG_DIR" && ! -e "$ACTIVE_LOCK" ]] || exit 1
# Refusal must preserve the original file, link or directory.
case KIND in
 empty) [[ -d "$HERMES_DATA/backups" ]] ;;
 broken) [[ -L "$HERMES_DATA/backups" ]] ;;
 file) [[ $(cat "$HERMES_DATA/backups") == preserve ]] ;;
 symlink) [[ -L "$HERMES_DATA/backups" ]] ;;
 writable) [[ $(file_stat mode "$HERMES_DATA/backups") == 777 ]] ;;
esac
'''.replace('KIND', kind))

    def test_unreadable_home_refused_before_first_setup(self):
        self.shell(r'''
mkdir "$HERMES_DATA"; printf existing > "$HERMES_DATA/data"
chmod 000 "$HERMES_DATA"
load_workspace; status=$?
chmod 700 "$HERMES_DATA"
[[ $status == 4 && ! -e "$METADATA" ]] || exit 1
[[ $(cat "$HERMES_DATA/data") == existing ]]
''')


    def test_mount_csv(self):
        result = self.shell(r'''mount_value '/tmp/a,b:"=\ space' '/home/a,b"/Docs'
''')
        import csv
        fields = next(csv.reader([result.stdout]))
        expected = ['type=bind', 'source=/tmp/a,b:"=\\ space', 'target=/home/a,b"/Docs', 'rw']
        if os.uname().sysname == 'Linux':
            expected.append('relabel=private')
        self.assertEqual(fields, expected)

    def test_unrelated_container_mount_is_allowed(self):
        self.shell(r'''
mkdir "$ACCOUNT_HOME/Unrelated"
inventory_snapshot() {
 json -n --arg source "$ACCOUNT_HOME/Unrelated" '[{id:("a"*64),state:"running",labels:{},mounts:[{source:$source,target:"/data"}]}]'
}
inventory_guard || exit
''')

    def test_devsy_home_mount_allowed_but_hermes_home_protected(self):
        self.shell(r'''
# Match the real layout: account home and the volume workspace are separate.
mkdir -m 700 "$ACCOUNT_HOME/account"
ACCOUNT_HOME=$ACCOUNT_HOME/account
set_paths "$WORKSPACE" || exit
test_mount_source=$ACCOUNT_HOME
inventory_snapshot() {
 json -n --arg source "$test_mount_source" '[{id:("a"*64),state:"running",labels:{},mounts:[{source:$source,target:$source}]}]'
}
inventory_guard || exit
test_mount_source=$WORKSPACE
inventory_guard || exit
test_mount_source=$HERMES_DATA/profiles
inventory_guard || exit
test_mount_source=$HERMES_DATA
inventory_guard; [[ $? == 4 ]]
''')

    def test_mount_guard_blocks_only_same_directories_and_workspace_labels(self):
        self.shell(r'''
# Model Home linked into the account directory that Toolbox mounts broadly.
mkdir -p "$ACCOUNT_HOME/real-home/profiles"
ln -s "$ACCOUNT_HOME/real-home" "$HERMES_DATA"
logical_home=$HERMES_DATA
HERMES_DATA=$(canonical_path "$logical_home") || exit
mkdir -p "$DOCUMENTS" "$USER_DOCUMENTS" "$BACKUPS"
ln -s "$BACKUPS" "$ACCOUNT_HOME/backups-alias"
ln -s "$DOCUMENTS" "$ACCOUNT_HOME/docs-alias"
ln -s "$USER_DOCUMENTS" "$ACCOUNT_HOME/user-docs-alias"
labelled=false
inventory_snapshot() {
 json -n --arg source "$test_mount_source" --arg state "$test_state" \
  --arg hash "$WORKSPACE_HASH" --argjson labelled "$labelled" \
  '[{id:("a"*64),state:$state,
     labels:(if $labelled then {"com.ezirius.hermesagent.workspace_hash":$hash} else {} end),
     mounts:[{source:$source,target:$source}]}]'
}
for test_state in running exited; do
 for test_mount_source in "$ACCOUNT_HOME" / "$HERMES_DATA-sibling" "$HERMES_DATA/profiles" "$DOCUMENTS/child" "$USER_DOCUMENTS/child"; do
  inventory_guard || exit
 done
 for test_mount_source in "$logical_home" "$HERMES_DATA" "$DOCUMENTS" "$USER_DOCUMENTS" "$ACCOUNT_HOME/docs-alias" "$ACCOUNT_HOME/user-docs-alias" "$BACKUPS" "$ACCOUNT_HOME/backups-alias"; do
  inventory_guard; [[ $? == 4 ]] || exit 1
 done
done
# Workspace labels still block a Hermes container even with only a broad mount.
test_mount_source=$ACCOUNT_HOME; labelled=true
inventory_guard; [[ $? == 4 ]]
''')

    def test_gateway_refusal(self):
        self.shell(r'''
mkdir "$HERMES_DATA"
printf '{"desired_state":"running"}' > "$HERMES_DATA/gateway_state.json"
if gateway_guard; then exit 1; fi
printf '{"desired_state":"stopped"}' > "$HERMES_DATA/gateway_state.json"
gateway_guard
''')

    def test_release_date(self):
        self.shell(r'''
release_key v2026.7.20 >/dev/null || exit
if release_key v2026.2.30; then exit 1; fi
if release_key latest; then exit 1; fi
''')

    def test_update_drift_never_falls_back(self):
        self.shell('''
resolve_online() { return 4; }
image_cached() { echo WRONG >&2; exit 99; }
choose_image '%s'
''' % json.dumps(IMAGE), expect=4)

    def test_update_menu_retries_and_allows_quit(self):
        self.shell(r'''
current='IMAGE_PLACEHOLDER'
newer=$(printf '%s' "$current" | json '.tag="v2026.9.14" | .digest=("sha256:"+("b"*64))')
resolve_online() { printf '%s\n' "$newer"; }
choose_image "$current" <<< $'wrong\n0\n3\n1' || exit
[[ "$SELECTED" == "$current" ]] || exit 1
choose_image "$current" <<< 2 || exit
[[ "$SELECTED" == "$newer" ]] || exit 1
for answer in q Q; do
 choose_image "$current" <<< "$answer"; [[ $? == 130 ]] || exit 1
done
choose_image "$current" < /dev/null; [[ $? == 130 ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_confirmation_honours_interruption_after_input(self):
        self.shell(r'''
# Simulate a signal delivered while read returns an affirmative answer.
read() { answer=y; INTERRUPTED=2; }
confirm 'Continue?'; [[ $? == 130 ]]
''')

    def test_offline_fallback_requires_cache(self):
        self.shell('''
resolve_online() { return 3; }
image_cached() { return 0; }
choose_image '%s' || exit
[[ "$SELECTED" != null && "$ALLOW_PULL" == false ]]
''' % json.dumps(IMAGE))

    def test_first_image_check_failure_explains_why(self):
        result = self.shell('resolve_online() { return 3; }; choose_image null', expect=3)
        self.assertIn('first setup/restore needs an online image selection', result.stderr)


    def test_inventory_official_schemas(self):
        self.shell(r'''
podman_json() {
 if [[ $1 == ps ]]; then json -n '[{Id:("a"*64),State:"running"}]'
 else json -n '[{Id:("a"*64),State:{Status:"running"},Config:{Labels:null},Mounts:[{Source:"/tmp/data",Destination:"/data"}]}]'; fi
}
inventory_one podman | json -e 'length==1 and .[0].labels=={} and .[0].mounts[0].source=="/tmp/data"' || exit
capture_json() {
 json -n '[{id:"sample",configuration:{id:"sample",labels:{},mounts:[{type:{virtiofs:{}},source:"/tmp/data",destination:"/data"},{type:{tmpfs:{}},source:"tmpfs",destination:"/tmp"}]},status:{state:"running"}}]'
}
inventory_one apple | json -e 'length==1 and .[0].mounts==[{source:"/tmp/data",target:"/data"}]' || exit
capture_json() { printf '[]\n'; }
[[ "$(inventory_one apple)" == '[]' ]] || exit
# Older Apple snapshots encode status as a string.
capture_json() { printf '[{"configuration":{"id":"sample","labels":{},"mounts":[]},"status":"stopped"}]\n'; }
inventory_one apple | json -e '.[0].state=="stopped"' || exit
capture_json() { printf '[{"configuration":{"id":"sample","labels":{},"mounts":[{"source":"relative","destination":"/data"}]},"status":"running"}]\n'; }
if inventory_one apple; then exit 1; fi
''')

    def test_native_output_allows_only_exact_s6_shutdown_notice(self):
        self.shell(r'''
mkdir -m 700 "$SCRATCH/backup"
printf '0\n' > "$SCRATCH/backup/exit"
printf 'Backup complete: /backup/example.zip\n' > "$SCRATCH/backup/stdout"
notice='s6-rc: warning: service s6rc-oneshot-runner is marked as essential, not stopping it'
printf '%s\n' "$notice" > "$SCRATCH/backup/stderr"
backup_output_ok "$SCRATCH/backup" example.zip || exit
CAPTURE_OUT=$SCRATCH/import.out; CAPTURE_ERR=$SCRATCH/backup/stderr
printf 'Import complete: 1 files restored\n' > "$CAPTURE_OUT"
backup_output_ok "$SCRATCH/backup" example.zip || exit
for line in '[config-migrate] WARNING: obsolete config' 'Warning: files skipped' 'SQLite safe copy failed' "$notice extra text"; do
 printf '%s\n%s\n' "$notice" "$line" > "$CAPTURE_ERR"
 if backup_output_ok "$SCRATCH/backup" example.zip; then exit 1; fi

done
''')


    def test_missing_or_linked_capture_logs_are_rejected(self):
        self.shell(r'''
mkdir -m 700 "$SCRATCH/backup"
printf '0\n' > "$SCRATCH/backup/exit"
printf 'Backup complete: /backup/example.zip\n' > "$SCRATCH/backup/stdout"
: > "$SCRATCH/backup/stderr"
backup_output_ok "$SCRATCH/backup" example.zip || exit
rm "$SCRATCH/backup/stderr"
if backup_output_ok "$SCRATCH/backup" example.zip; then exit 1; fi
''')

    def test_inventory_shape_and_mismatch(self):
        self.shell(r'''
podman_json() {
 if [[ $1 == ps ]]; then printf '[{"Id":"%064d","State":"running"}]\n' 0
 else printf '[]\n'; fi
}
if inventory_one podman; then exit 1; fi
''')


    def test_incomplete_backup_zero_exit_refused(self):
        self.shell(r'''
mkdir "$SCRATCH/backup"
printf 0 > "$SCRATCH/backup/exit"
printf 'Backup incomplete: /backup/hermes-backup-2026-09-12-000000.zip\nWarnings: skipped\n' > "$SCRATCH/backup/stdout"
: > "$SCRATCH/backup/stderr"
if backup_output_ok "$SCRATCH/backup" hermes-backup-2026-09-12-000000.zip; then exit 1; fi
''')

    def test_real_zip_and_corruption(self):
        def setup(root):
            with zipfile.ZipFile(root/'good.zip', 'w') as archive:
                archive.writestr('config.yaml', 'model: fixture')
                archive.writestr('data.txt', 'hello')
            os.chmod(root/'good.zip', 0o600)
        self.shell(r'''
validate_zip "$2/good.zip" || exit
printf broken > "$2/good.zip"
if validate_zip "$2/good.zip"; then exit 1; fi
''', setup=setup)

    def test_retention_fourteen_days_and_foreign_preservation(self):
        def setup(root):
            with zipfile.ZipFile(root/'fixture.zip', 'w') as archive:
                archive.writestr('config.yaml', 'model: fixture')
            os.chmod(root/'fixture.zip', 0o600)
        self.shell('''
load_workspace && acquire_lock && prepare_directories || exit
now=$(json -n '"2026-09-14T00:00:00Z" | fromdateiso8601')
backup_cutoff() { printf '%%s\n' "$((now-14*86400))"; }
for ((i=1;i<=19;i++)); do
 operation=$(printf '%%032x' "$i")
 directory=$BACKUPS/$operation
 mkdir "$directory"
 cp "$2/fixture.zip" "$directory/hermes-backup-2026-09-12-000000.zip"
 hash=$(file_hash "$directory/hermes-backup-2026-09-12-000000.zip")
 age=$((i-1)); [[ "$i" != 19 ]] || age=-1
 captured=$(json -nr --argjson seconds "$((now-age*86400))" '$seconds | todateiso8601')
 receipt=$(json -n --arg workspace "$WORKSPACE" --arg operation "$operation" --arg hash "$hash" --arg captured "$captured" --argjson sequence "$i" --argjson source '%s' '{schema:1,workspace:$workspace,operation:$operation,sequence:$sequence,name:"hermes-backup-2026-09-12-000000.zip",hash:$hash,captured:$captured,source:$source,validated:true}')
 # Exercise new receipts alongside preserved legacy receipts.
 if ((i %% 2 == 0)); then
  receipt=$(printf '%%s' "$receipt" | json '{schema:2,workspace,operation,name,hash,captured,source:(.source|{tag,platform,digest})}') || exit
 fi
 atomic_record "$directory/receipt.json" "$receipt" || exit
done
printf preserve > "$BACKUPS/foreign.zip"
retain_backups || exit
count=$(find "$BACKUPS" -name receipt.json | wc -l | tr -d ' ')
[[ $count == 16 && $(cat "$BACKUPS/foreign.zip") == preserve ]] || exit 1
# All recent backups survive, including the exact 14-day boundary and a future date.
[[ -d "$BACKUPS/$(printf '%%032x' 15)" && -d "$BACKUPS/$(printf '%%032x' 19)" ]] || exit 1
[[ ! -e "$BACKUPS/$(printf '%%032x' 16)" ]]
''' % json.dumps(LEGACY_IMAGE), setup=setup)


    @staticmethod
    def zip_fixture(root):
        with zipfile.ZipFile(root/'fixture.zip', 'w') as archive:
            archive.writestr('config.yaml', 'model: fixture')
            archive.writestr('data.txt', 'retained')
        os.chmod(root/'fixture.zip', 0o600)


    def test_capture_timeout(self):
        self.shell(r'''
run_capture 1 /bin/sh -c 'echo $$ > "$1"; exec /bin/sleep 5' sh "$SCRATCH/child.pid"
[[ $? == 3 && -z "$CHILD_PID" ]] || exit 1
! kill -0 "$(cat "$SCRATCH/child.pid")" 2>/dev/null
''')


    def test_safe_docs_mode_preserved(self):
        self.shell(r'''
mkdir -m 755 "$DOCUMENTS"
load_workspace && acquire_lock && prepare_directories || exit
[[ $(file_stat mode "$DOCUMENTS") == 755 ]]
''')

    def test_unsafe_zip_names(self):
        for name in ('../escape', 'backups/old.zip'):
            with self.subTest(name=name):
                def setup(root):
                    with zipfile.ZipFile(root/'bad.zip', 'w') as archive:
                        archive.writestr('config.yaml', 'x')
                        archive.writestr(name, 'bad')
                    os.chmod(root/'bad.zip', 0o600)
                self.shell('if validate_zip "$2/bad.zip"; then exit 1; fi', setup=setup)

    def test_zip_file_directory_collisions(self):
        # ZIP integrity checks alone accept paths which cannot coexist on disk.
        for names in [('config.yaml/child',), ('config.yaml/',),
                      ('other', 'other/nested/file')]:
            with self.subTest(names=names):
                def setup(root):
                    with zipfile.ZipFile(root/'bad.zip', 'w') as archive:
                        archive.writestr('config.yaml', 'x')
                        for name in names:
                            archive.writestr(name, '')
                    os.chmod(root/'bad.zip', 0o600)
                self.shell('if validate_zip "$2/bad.zip"; then exit 1; fi', setup=setup)

    def test_zip_explicit_and_implicit_directories(self):
        def setup(root):
            with zipfile.ZipFile(root/'good.zip', 'w') as archive:
                archive.writestr('config.yaml', 'x')
                archive.writestr('sessions/', '')
                archive.writestr('sessions/one.json', '{}')
                archive.writestr('memories/nested/note.md', 'hello')
            os.chmod(root/'good.zip', 0o600)
        self.shell('validate_zip "$2/good.zip"', setup=setup)

    def test_session_changes_directory_after_init_with_literal_arguments(self):
        self.shell(r'''
mkdir -m 700 "$SCRATCH/bin"
printf '#!/bin/sh\npwd -P\nprintf "argc=%%s\\n" "$#"\nprintf "%%s\\n" "$@"\n' > "$SCRATCH/bin/hermes"
chmod 700 "$SCRATCH/bin/hermes"
CONTAINER_DOCS="$SCRATCH/Docs with spaces and 'quotes'"
mkdir "$CONTAINER_DOCS"
export TERMINAL_CWD="$CONTAINER_DOCS"
export PATH="$SCRATCH/bin:$PATH"
for action in chat setup; do
 build_runtime 'IMAGE_PLACEHOLDER' "$action" "$HERMES_DATA" || exit
 length=4
 [[ "$action" != setup ]] || length=5
 command=("${RUNTIME[@]:${#RUNTIME[@]}-length}")
 "${command[@]}" > "$SCRATCH/result" || exit
 grep -Fx "$CONTAINER_DOCS" "$SCRATCH/result" || exit
 if [[ "$action" == setup ]]; then
  grep -Fx 'argc=1' "$SCRATCH/result" && grep -Fx setup "$SCRATCH/result" || exit
 else grep -Fx 'argc=0' "$SCRATCH/result" || exit; fi
done
TERMINAL_CWD=$SCRATCH/missing
if "${command[@]}" > "$SCRATCH/result"; then exit 1; fi
[[ ! -s "$SCRATCH/result" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_dashboard_ports_and_saved_authentication(self):
        self.shell(r'''
image='IMAGE_PLACEHOLDER'
for account in ezirius Ezirius EZIRIUS nala Nala NALA someone; do
 ACCOUNT_USER=$account
 case "$account" in
  ezirius|Ezirius|EZIRIUS) expected=19119 ;;
  nala|Nala|NALA) expected=29119 ;;
  *) expected=59119 ;;
 esac
 build_runtime "$image" service "$HERMES_DATA" || exit
 printf '%s\n' "${RUNTIME[@]}" > "$SCRATCH/argv"
 grep -Fx "127.0.0.1:$expected:9119" "$SCRATCH/argv" || exit
 [[ $(grep -c '^--publish$' "$SCRATCH/argv") == 1 ]] || exit 1
 [[ $(grep -c '^--mount$' "$SCRATCH/argv") == 4 ]] || exit 1
 grep -Fx -- '-d' "$SCRATCH/argv" || exit
 grep -Fx -- '--restart=unless-stopped' "$SCRATCH/argv" || exit
 if grep -E '^--rm$|^-it$' "$SCRATCH/argv"; then exit 1; fi
 # Use native saved authentication and the supervised dashboard, never bypass it.
 grep -Fx 'HERMES_DASHBOARD=1' "$SCRATCH/argv" || exit
 if grep -E 'BASIC_AUTH|OAUTH|INSECURE|--insecure' "$SCRATCH/argv"; then exit 1; fi
 printf '%s\n' gateway run > "$SCRATCH/expected"
 tail -2 "$SCRATCH/argv" > "$SCRATCH/actual"
 cmp "$SCRATCH/expected" "$SCRATCH/actual" || exit
done
for action in chat setup backup import; do
 build_runtime "$image" "$action" "$HERMES_DATA" "$BACKUPS/input.zip" || exit
 printf '%s\n' "${RUNTIME[@]}" > "$SCRATCH/argv"
 if grep -Fx -- '--publish' "$SCRATCH/argv"; then exit 1; fi
done
parse_args start EZIRIUS || exit
[[ "$ACTION" == start && "$WORKSPACE_ARGUMENT" == EZIRIUS ]] || exit 1
parse_args start || exit
[[ "$ACTION" == start && -z "$WORKSPACE_ARGUMENT" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_service_workflow_starts_once_and_reuses_for_chat_and_start(self):
        self.shell(r'''
IMAGE_CONFIG='IMAGE_PLACEHOLDER'; PODMAN=podman
started=false; starts=0; chats=0
select_workspace() { return 0; }; load_workspace() { return 0; }
inventory_guard() { return 0; }; gateway_guard() { return 0; }
check_service_database() { return 0; }; check_service_image() { return 0; }
host_readiness() { return 0; }
acquire_lock() { return 0; }; verify_lock() { return 0; }
prepare_directories() { return 0; }; verify_vm_shares() { return 0; }
ensure_image() { return 0; }; wait_dashboard() { return 0; }
read_image() { printf '%s' "$IMAGE_CONFIG"; }
choose_image() { SELECTED=$IMAGE_CONFIG; ALLOW_PULL=false; }
find_service() {
 SERVICE_ID=; SERVICE_STATE=
 if $started; then SERVICE_ID=$(printf 'a%.0s' {1..64}); SERVICE_STATE=running; fi
}
run_capture() { [[ "$3" == run ]] || return 99; started=true; ((starts+=1)); }
service_chat() { [[ -n "$SERVICE_ID" ]] || return 4; ((chats+=1)); }
ACTION=start
operate_service || exit
ACTION=chat
operate_service || exit
[[ "$starts" == 1 && "$chats" == 1 ]] || exit 1
# A dashboard URL remains available while another terminal holds the chat lock.
acquire_lock() { return 99; }
ACTION=start
operate_service || exit
[[ "$starts" == 1 && "$chats" == 1 ]] || exit 1
# A service appearing during an accepted update must still block replacement.
started=false
acquire_lock() { return 0; }
filevault_prompt() { return 0; }
choose_image() {
 SELECTED=$(printf '%s' "$IMAGE_CONFIG" | json '.tag="v2026.7.21"')
 ALLOW_PULL=true; started=true
}
create_backup() { return 99; }
operate_service; [[ $? == 4 ]] || exit 1
[[ "$starts" == 1 && "$chats" == 1 ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_approved_upgrade_stops_backs_up_and_restarts_in_order(self):
        result = self.shell(r'''
old='IMAGE_PLACEHOLDER'
new=$(printf '%s' "$old" | json '.tag="v2026.7.21" | .digest=("sha256:"+("b"*64))')
select_workspace() { return 0; }; load_workspace() { return 0; }
verify_source_paths() { return 0; }; inventory_guard() { return 0; }
gateway_guard() { return 0; }; check_service_image() { return 0; }
host_readiness() { return 0; }; verify_lock() { return 0; }
prepare_directories() { return 0; }; verify_vm_shares() { return 0; }
read_image() { printf '%s' "$IMAGE_CONFIG"; }
resolve_online() { printf '%s' "$new"; }
event() { printf '%s\n' "$1" >> "$SCRATCH/events"; }
find_service() {
 SERVICE_ID=; SERVICE_STATE=; SERVICE_EXECS=0
 if $running; then SERVICE_ID=$observed_service; SERVICE_STATE=running; fi
}
acquire_lock() { [[ "$scenario" != locked ]] || return 4; }
ensure_image() {
 if [[ "$scenario" == pull_failed && "$2" == true ]]; then return 3; fi
 if [[ "$scenario" == old_image_missing && "$2" == false ]]; then return 3; fi
}
filevault_prompt() {
 [[ "$scenario" != encryption_declined ]] || return 130
 [[ "$scenario" != replaced ]] || observed_service=replacement
 return 0
}
stop_service() {
 event stop
 [[ "$scenario" != stop_failed ]] || return 4
 running=false; SERVICE_ID=; ALLOWED_SERVICE_ID=
}
create_backup() {
 [[ "$1" == "$old" && "$running" == false ]] || return 99
 event backup
 [[ "$scenario" != backup_failed ]] || return 4
}
save_image() { event select; IMAGE_CONFIG=$1; }
start_service() {
 event start
 [[ "$scenario" != start_failed ]] || return 3
 running=true
}
wait_dashboard() { event ready; }
show_dashboard() { return 0; }; retain_backups() { event prune; }
service_chat() { event chat; }
for scenario in approved approved_start stopped continue cancelled locked pull_failed old_image_missing encryption_declined replaced stop_failed backup_failed start_failed; do
 IMAGE_CONFIG=$old; running=true; ACTION=chat; observed_service=original
 [[ "$scenario" != approved_start ]] || ACTION=start
 [[ "$scenario" != stopped ]] || running=false
 : > "$SCRATCH/events"
 answer=2
 [[ "$scenario" != continue ]] || answer=1
 [[ "$scenario" != cancelled ]] || answer=q
 operate_service <<< "$answer"; status=$?
 events=$(cat "$SCRATCH/events")
 case "$scenario" in
  approved) [[ "$status" == 0 && "$events" == $'stop\nbackup\nselect\nstart\nready\nprune\nchat' && "$IMAGE_CONFIG" == "$new" ]] || exit 1 ;;
  approved_start) [[ "$status" == 0 && "$events" == $'stop\nbackup\nselect\nstart\nready\nprune' && "$IMAGE_CONFIG" == "$new" ]] || exit 1 ;;
  stopped) [[ "$status" == 0 && "$events" == $'backup\nselect\nstart\nready\nprune\nchat' && "$IMAGE_CONFIG" == "$new" ]] || exit 1 ;;
  continue) [[ "$status" == 0 && "$events" == $'start\nready\nchat' && "$IMAGE_CONFIG" == "$old" ]] || exit 1 ;;
  cancelled|encryption_declined) [[ "$status" == 130 && -z "$events" && "$running" == true && "$IMAGE_CONFIG" == "$old" ]] || exit 1 ;;
  locked|replaced) [[ "$status" == 4 && -z "$events" && "$running" == true && "$IMAGE_CONFIG" == "$old" ]] || exit 1 ;;
  pull_failed|old_image_missing) [[ "$status" == 3 && -z "$events" && "$running" == true && "$IMAGE_CONFIG" == "$old" ]] || exit 1 ;;
  stop_failed) [[ "$status" == 4 && "$events" == stop && "$IMAGE_CONFIG" == "$old" ]] || exit 1 ;;
  backup_failed) [[ "$status" == 4 && "$events" == $'stop\nbackup' && "$IMAGE_CONFIG" == "$old" ]] || exit 1 ;;
  start_failed) [[ "$status" == 3 && "$events" == $'stop\nbackup\nselect\nstart' && "$IMAGE_CONFIG" == "$new" ]] || exit 1 ;;
 esac
done
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))
        self.assertIn('Update will stop Hermes if needed, create a verified backup', result.stderr)

    def test_service_identity_rejects_foreign_or_changed_containers(self):
        self.shell(r'''
IMAGE_CONFIG='IMAGE_PLACEHOLDER'
cid=$(printf 'a%.0s' {1..64})
json -n --arg cid "$cid" --arg name "$(service_name)" --arg image "$IMAGE@sha256:$(printf 'a%.0s' {1..64})" \
 --arg hash "$WORKSPACE_HASH" --arg uid "$OPERATOR_UID" --arg gid "$OPERATOR_GID" \
 --arg home "$HERMES_DATA" --arg backups "$BACKUPS" --arg docs "$DOCUMENTS" --arg user "$USER_DOCUMENTS" \
 --arg dt "$CONTAINER_DOCS" --arg ut "$CONTAINER_USER_DOCS" '
 [{Id:$cid,Name:$name,ImageName:$image,ExecIDs:[],State:{Status:"running"},
 Config:{Cmd:["gateway","run"],WorkingDir:"/opt/data",Labels:{
 "com.ezirius.hermesagent.managed":"true","com.ezirius.hermesagent.action":"service",
 "com.ezirius.hermesagent.workspace_hash":$hash,"com.ezirius.hermesagent.digest":("sha256:"+("a"*64))},
 Env:["HERMES_DASHBOARD=1","HERMES_DASHBOARD_HOST=0.0.0.0","HERMES_DASHBOARD_PORT=9119",
 "HERMES_UID="+$uid,"HERMES_GID="+$gid,"TERMINAL_ENV=local",
 "TERMINAL_CWD="+$dt,"HERMES_WRITE_SAFE_ROOT="+$dt]},
 HostConfig:{Memory:2147483648,ShmSize:536870912,NanoCpus:1000000000,AutoRemove:false,Privileged:false,RestartPolicy:{Name:"unless-stopped"},
 PortBindings:{"9119/tcp":[{HostIp:"127.0.0.1",HostPort:"59119"}]}},
 Mounts:([{Source:$home,Destination:"/opt/data"},{Source:$backups,Destination:"/opt/data/backups"},
 {Source:$docs,Destination:$dt},{Source:$user,Destination:$ut}] | map(.+{Type:"bind",RW:true}))}]' > "$SCRATCH/original"
# An old creation timestamp must still be discovered and retained.
json '.[0].Name |= sub("[0-9]{8}T[0-9]{6}Z";"20260101T010203Z")' < "$SCRATCH/original" > "$SCRATCH/older"
mv "$SCRATCH/older" "$SCRATCH/original"
cp "$SCRATCH/original" "$SCRATCH/inspect"
podman_json() {
 if [[ "$1" == ps ]]; then
  [[ "$*" == *"label=com.ezirius.hermesagent.workspace_hash=$WORKSPACE_HASH"* &&
     "$*" == *"label=com.ezirius.hermesagent.action=service"* ]] || return 99
  json -n --arg cid "$cid" '[{Id:$cid}]'
 else cat "$SCRATCH/inspect"; fi
}
find_service || exit
[[ "$SERVICE_NAME" == "$(json -r '.[0].Name' < "$SCRATCH/original")" ]] || exit 1
[[ "$SERVICE_ID" == "$cid" && "$SERVICE_STATE" == running ]] || exit 1
# Podman can also describe a CPU limit using quota and period.
json '.[0].HostConfig |= (.NanoCpus=0 | .CpuPeriod=100000 | .CpuQuota=100000)' < "$SCRATCH/original" > "$SCRATCH/inspect"
find_service || exit
json '.[0].HostConfig.NanoCpus=2000000000' < "$SCRATCH/inspect" > "$SCRATCH/conflicting"
mv "$SCRATCH/conflicting" "$SCRATCH/inspect"
if find_service; then exit 1; fi
for change in '.[0].Name="hermes-workspace"' '.[0].Name |= sub("-service-";"-chat-")' '.[0].Config.Labels={}' '.[0].Config.Cmd=["sleep","infinity"]' \
 '.[0].Mounts[0].Source="/wrong"' '.[0].HostConfig.Privileged=true' \
 '.[0].HostConfig.PortBindings["9119/tcp"][0].HostIp="0.0.0.0"' \
 '.[0].ImageName="other"' '.[0].Config.Env=[]' \
 '.[0].Config.Env |= join(" ")' '.[0].Config.Env += ["HERMES_DASHBOARD=0"]' \
 '.[0].Config.Env |= map(select(startswith("TERMINAL_ENV=") | not))' \
 '.[0].Config.Env |= map(if startswith("TERMINAL_ENV=") then "TERMINAL_ENV=docker" else . end)' \
 '.[0].Config.Env |= map(if startswith("TERMINAL_CWD=") then "TERMINAL_CWD=/tmp" else . end)' \
 '.[0].Config.Env |= map(if startswith("HERMES_WRITE_SAFE_ROOT=") then "HERMES_WRITE_SAFE_ROOT=/" else . end)' \
 '.[0].HostConfig.Memory=0' '.[0].HostConfig.ShmSize=0' '.[0].HostConfig.NanoCpus=0' \
 '.[0].HostConfig.RestartPolicy.Name="always"' \
 '.[0].ExecIDs=false' '.[0].ExecIDs=[false]'; do
 json "$change" < "$SCRATCH/original" > "$SCRATCH/inspect"
 find_service; [[ $? == 4 ]] || exit 1
done
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_full_container_names_share_the_same_convention(self):
        self.shell(r'''
image='IMAGE_PLACEHOLDER'
for action in setup backup import service; do
 build_runtime "$image" "$action" "$HERMES_DATA" "$BACKUPS/input.zip" || exit
 name=
 for ((i=0; i<${#RUNTIME[@]}; i++)); do
  [[ "${RUNTIME[i]}" != --name ]] || name=${RUNTIME[i+1]}
 done
 prefix=hermes-v2026.7.20-
 suffix=-workspace-$action-${WORKSPACE_HASH:0:12}
 [[ "$name" == "$prefix"*"$suffix" ]] || exit 1
 stamp=${name#"$prefix"}; stamp=${stamp%"$suffix"}
 [[ "$stamp" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || exit 1
 if [[ "$action" == service ]]; then [[ "$SERVICE_NAME" == "$name" ]] || exit 1; fi
done
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_service_discovery_rejects_multiple_matches(self):
        self.shell(r'''
podman_json() { json -n '[{Id:("a"*64)},{Id:("b"*64)}]'; }
find_service; [[ $? == 4 ]]
''')

    def test_service_discovery_rejects_malformed_lists(self):
        result = self.shell(r'''
for listing in null '{}' '""' '[{"Id":null}]' '[{"Id":12}]'; do
 podman_json() { printf '%s' "$listing"; }
 find_service; [[ $? == 4 ]] || exit 1
done
podman_json() { printf '[]'; }
find_service || exit
[[ -z "$SERVICE_ID" && "$SERVICE_EXECS" == 0 ]]
''')
        self.assertIn('service inventory is malformed', result.stderr)

    def test_service_refresh_refuses_changed_paths_and_config(self):
        self.shell(r'''
IMAGE_CONFIG='IMAGE_PLACEHOLDER'
paths_ok=true; current=$IMAGE_CONFIG; inspected=false
verify_source_paths() { $paths_ok; }
read_image() { printf '%s' "$current"; }
find_service() { inspected=true; SERVICE_ID=fixture; }
inventory_guard() { [[ "$ALLOWED_SERVICE_ID" == fixture ]]; }
refresh_service || exit
inspected=false; paths_ok=false
if refresh_service; then exit 1; fi
[[ "$inspected" == false ]] || exit 1
paths_ok=true; current=null
if refresh_service; then exit 1; fi
[[ "$inspected" == false ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_dashboard_response_requires_same_running_service(self):
        self.shell(r'''
printf '{"auth_required":true,"auth_providers":["basic"]}' > "$SCRATCH/status"
CURL=curl
run_capture() { CAPTURE_OUT=$SCRATCH/status; }
refresh_service() { SERVICE_ID=$observed_id; SERVICE_STATE=$observed_state; }
for observed_id in original replacement ''; do
 for observed_state in running exited; do
  SERVICE_ID=original
  wait_dashboard; status=$?
  if [[ "$observed_id" == original && "$observed_state" == running ]]; then
   [[ "$status" == 0 ]] || exit 1
  else [[ "$status" == 4 ]] || exit 1; fi
 done
done
''')

    def test_dashboard_authentication_requires_unambiguous_json(self):
        self.shell(r'''
for response in \
 '{"auth_required":false,"auth_required":true,"auth_providers":["basic"]}' \
 '{"auth_required":false} {"auth_required":true,"auth_providers":["basic"]}' \
 '{"auth_required":true,"auth_providers":[null]}' \
 '{"auth_required":true,"auth_providers":[""]}' \
 '{"auth_required":true,"auth_providers":[]}' \
 '{"auth_required":false,"auth_providers":["basic"]}'; do
 printf '%s' "$response" > "$SCRATCH/status"
 if dashboard_authenticated "$SCRATCH/status"; then exit 1; fi
done
printf '{"auth_required":true,"auth_providers":["basic","nous"]}' > "$SCRATCH/status"
dashboard_authenticated "$SCRATCH/status"
''')

    def test_mac_service_image_requires_cross_vm_database_protection(self):
        self.shell(r'''
HOST_OS=Darwin
if check_service_image '{"tag":"v2026.9.11"}'; then exit 1; fi
check_service_image '{"tag":"v2026.9.14"}' || exit
check_service_image '{"tag":"v2026.9.15"}' || exit
HOST_OS=Linux
check_service_image '{"tag":"v2026.9.11"}'
''')

    def test_service_inventory_exemption_is_limited_to_one_id(self):
        self.shell(r'''
ALLOWED_SERVICE_ID=$(printf 'a%.0s' {1..64}); extra=false
inventory_snapshot() {
 json -n --arg cid "$ALLOWED_SERVICE_ID" --arg hash "$WORKSPACE_HASH" --arg home "$HERMES_DATA" --argjson extra "$extra" '
 [{id:$cid,state:"running",labels:{"com.ezirius.hermesagent.workspace_hash":$hash},mounts:[{source:$home,target:"/opt/data"}]}]
 + (if $extra then [{id:("b"*64),state:"running",labels:{},mounts:[{source:$home,target:"/data"}]}] else [] end)'
}
inventory_guard || exit
extra=true
inventory_guard; [[ $? == 4 ]]
''')

    def test_service_reuse_and_start_keep_container(self):
        self.shell(r'''
verify_lock() { return 0; }; gateway_guard() { return 0; }
inventory_guard() { return 0; }; check_service_database() { return 0; }
SERVICE_ID=$(printf 'a%.0s' {1..64}); SERVICE_STATE=running
run_capture() { echo unexpected >&2; return 99; }
start_service || exit
SERVICE_ID=; SERVICE_STATE=
build_runtime() { RUNTIME=(run -d); RUN_CID=$SCRATCH/cid; }
run_capture() { [[ "$3" == run && "$4" == -d ]]; }
find_service() { SERVICE_ID=$(printf 'b%.0s' {1..64}); SERVICE_STATE=running; }
PODMAN=podman
start_service || exit
[[ "$ALLOWED_SERVICE_ID" == "$SERVICE_ID" && -z "$RUN_CID" && "$RESIDUAL_UNKNOWN" == false ]]
''')

    def test_service_stop_order_and_active_exec_refusal(self):
        self.shell(r'''
verify_lock() { return 0; }; inventory_guard() { return 0; }
gateway_guard() { echo guard >> "$SCRATCH/calls"; }
SERVICE_ID=$(printf 'a%.0s' {1..64}); SERVICE_STATE=running; SERVICE_EXECS=1
PODMAN=podman
run_capture() { printf '%s\n' "$*" >> "$SCRATCH/calls"; }
if stop_service; then exit 1; fi
[[ ! -e "$SCRATCH/calls" ]] || exit 1
SERVICE_EXECS=0
stop_service || exit
[[ -z "$SERVICE_ID" && -z "$ALLOWED_SERVICE_ID" ]] || exit 1
[[ $(sed -n '1p' "$SCRATCH/calls") == *'s6-svc -d /run/service/dashboard' ]] || exit 1
[[ $(sed -n '2p' "$SCRATCH/calls") == *'s6-svwait -d -t 10000 /run/service/dashboard' ]] || exit 1
[[ $(sed -n '3p' "$SCRATCH/calls") == *'hermes gateway stop --all' ]] || exit 1
[[ $(sed -n '4p' "$SCRATCH/calls") == guard ]] || exit 1
[[ $(sed -n '5p' "$SCRATCH/calls") == *'stop --time 30 '* ]] || exit 1
[[ $(sed -n '6p' "$SCRATCH/calls") == *'rm '* ]]
''')

    def test_service_stop_failure_preserves_container(self):
        self.shell(r'''
verify_lock() { return 0; }; inventory_guard() { return 0; }
SERVICE_ID=$(printf 'a%.0s' {1..64}); SERVICE_STATE=running; SERVICE_EXECS=0
PODMAN=podman
run_capture() { [[ "$*" != *'gateway stop --all'* ]] || return 3; [[ "$3" == exec ]]; }
stop_service; [[ $? == 3 && -n "$SERVICE_ID" ]]
''')

    def test_service_chat_uses_exec_and_never_stops_container(self):
        self.shell(r'''
verify_lock() { return 0; }; inventory_guard() { return 0; }
SERVICE_ID=$(printf 'a%.0s' {1..64}); PODMAN=$SCRATCH/podman
printf '#!/bin/sh\nprintf "%%s\\n" "$@" > "$HOME/chat-args"\n' > "$PODMAN"
chmod 700 "$PODMAN"
service_chat || exit
[[ $(head -1 "$ACCOUNT_HOME/chat-args") == exec ]] || exit 1
grep -Fx "$SERVICE_ID" "$ACCOUNT_HOME/chat-args" || exit
grep -Fx "$CONTAINER_DOCS" "$ACCOUNT_HOME/chat-args" || exit
[[ $(tail -1 "$ACCOUNT_HOME/chat-args") == hermes && "$RESIDUAL_UNKNOWN" == false ]]
''')

    def test_service_chat_cancellation_during_checks_never_launches(self):
        self.shell(r'''
verify_lock() { return 0; }
inventory_guard() { INTERRUPTED=15; return 0; }
PODMAN=$SCRATCH/podman
printf '#!/bin/sh\ntouch "$HOME/unexpected-chat"\n' > "$PODMAN"
chmod 700 "$PODMAN"
service_chat; [[ $? == 143 ]] || exit 1
[[ -z "$CHILD_PID" && ! -e "$ACCOUNT_HOME/unexpected-chat" ]]
''')

    def test_service_command_failures_explain_step_and_stop_sequence(self):
        result = self.shell(r'''
verify_lock() { return 0; }; inventory_guard() { return 0; }
gateway_guard() { return 0; }; check_service_database() { return 0; }
PODMAN=podman; SERVICE_EXECS=0
run_capture() {
 calls=$((calls+1))
 CAPTURE_OUT=$SCRATCH/service.stdout; CAPTURE_ERR=$SCRATCH/service.stderr
 printf 'private-output-sentinel\n' > "$CAPTURE_OUT"
 printf 'private-error-sentinel\n' > "$CAPTURE_ERR"
 ((calls!=failed_step))
}
# Every failed shutdown step must prevent all later commands.
for failed_step in 1 2 3 4 5; do
 calls=0; SERVICE_ID=fixture; SERVICE_STATE=running; RESIDUAL_UNKNOWN=false
 stop_service; [[ $? == 1 ]] || exit 1
 [[ "$calls" == "$failed_step" && "$SERVICE_ID" == fixture && "$RESIDUAL_UNKNOWN" == true ]] || exit 1
done
calls=0; failed_step=1; SERVICE_STATE=exited
start_service; [[ $? == 1 && "$RESIDUAL_UNKNOWN" == true ]] || exit 1
# Cancellation is not reported as a container failure.
run_capture() { INTERRUPTED=2; return 130; }
service_command 'cancelled-step' 10 stop fixture; [[ $? == 130 ]]
''')
        for step in ('stop the dashboard', 'wait for the dashboard to stop',
                     'stop the gateways', 'stop the container',
                     'remove the stopped container', 'start the existing container'):
            self.assertIn('could not '+step+' (status 1); inspect ', result.stderr)
        self.assertNotIn('private-output-sentinel', result.stderr)
        self.assertNotIn('private-error-sentinel', result.stderr)
        self.assertNotIn('cancelled-step', result.stderr)

    def test_service_blocks_wal_before_concurrent_mac_use(self):
        self.shell(r'''
mkdir -p "$HERMES_DATA"
HOST_OS=Darwin
for relative in state.db kanban.db profiles/test/state.db profiles/.hidden/state.db kanban/boards/tasks/kanban.db profiles/test/kanban/boards/tasks/kanban.db; do
 path=$HERMES_DATA/$relative
 mkdir -p "${path%/*}"
 printf 'SQLite format 3\000\020\000\002\002' > "$path"
 if check_service_database; then exit 1; fi
 printf 'SQLite format 3\000\020\000\001\001' > "$path"
 check_service_database || exit
done
''')

    def test_runtime_literal_flags(self):
        self.shell(r'''
image='IMAGE_PLACEHOLDER'
build_runtime "$image" chat "$HERMES_DATA" || exit
printf '%s\n' "${RUNTIME[@]}" > "$SCRATCH/argv"
grep -Fx -- '--pull=never' "$SCRATCH/argv" || exit
grep -Fx -- '-it' "$SCRATCH/argv" || exit
if grep -Fx -- '--privileged' "$SCRATCH/argv"; then exit 1; fi
grep -Fx -- "$IMAGE@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" "$SCRATCH/argv" || exit
[[ ${RUNTIME[${#RUNTIME[@]}-1]} == hermes-launcher ]]
'''.replace('IMAGE_PLACEHOLDER',json.dumps(IMAGE)))

    def test_pagination_malformed_link_refused(self):
        self.shell(r'''
http_get() { HTTP_TYPE=application/json; HTTP_BODY='[]'; HTTP_LINK='garbage'; }
if github_releases; then exit 1; fi
''')


    def test_github_complete_pagination(self):
        self.shell(r'''
http_get() {
 HTTP_TYPE=application/json
 if [[ "$1" == *'page=2' ]]; then
   HTTP_BODY='[{"id":2,"draft":false,"prerelease":false,"tag_name":"v2026.7.21","published_at":"2026-07-21T00:00:00Z"}]'
   HTTP_LINK=
 else
   HTTP_BODY='[{"id":1,"draft":false,"prerelease":false,"tag_name":"v2026.7.20","published_at":"2026-07-20T00:00:00Z"}]'
   HTTP_LINK='<https://api.github.com/repos/NousResearch/hermes-agent/releases?per_page=100&page=2>; rel="next"'
 fi
}
release=$(github_releases) || exit
[[ $(printf '%s' "$release" | json -r .tag) == v2026.7.21 ]]
''')

    def test_github_skipped_page_refused(self):
        self.shell(r'''
http_get() {
 HTTP_TYPE=application/json; HTTP_BODY='[]'
 HTTP_LINK='<https://api.github.com/repos/NousResearch/hermes-agent/releases?per_page=100&page=3>; rel="next"'
}
if github_releases; then exit 1; fi
''')

    def test_registry_pair_validation(self):
        self.shell(r'''
http_get() {
 HTTP_TYPE=application/vnd.oci.image.index.v1+json
 HTTP_BODY=$(json -n '{schemaVersion:2, manifests:[{digest:("sha256:"+("a"*64)),size:123,mediaType:"application/vnd.oci.image.manifest.v1+json",platform:{os:"linux",architecture:"arm64"}},{digest:("sha256:"+("b"*64)),size:123,mediaType:"application/vnd.oci.image.manifest.v1+json",platform:{os:"linux",architecture:"amd64"}}]}')
}
children=$(registry_children v2026.7.20 token) || exit
[[ $(printf '%s' "$children" | json 'keys|length') == 2 ]]
''')

    def test_capture_signal_forwarding(self):
        self.shell(r'''
trap 'interrupted 15' TERM
(sleep 0.2; kill -TERM $$) &
run_capture 10 /bin/sleep 20
status=$?
[[ $status == 143 && -z "$CHILD_PID" ]]
''')


    def test_image_unknown_fields_refused(self):
        image=dict(IMAGE, published='2026-02-30T00:00:00Z')
        self.shell("if validate_record image '%s'; then exit 1; fi" % json.dumps(image))

    def test_gateway_ambiguous_states_refused(self):
        self.shell(r'''
mkdir "$HERMES_DATA"
for record in '{}' '{"desired_state":false,"gateway_state":"stopped"}' '{"desired_state":null}' '{"gateway_state":"starting"}'; do
 printf '%s' "$record" > "$HERMES_DATA/gateway_state.json"
 if gateway_guard; then exit 1; fi
done
printf '{"gateway_state":"stopped"}' > "$HERMES_DATA/gateway_state.json"
gateway_guard
''')

    def test_gateway_hidden_profile_refused(self):
        self.shell(r'''
mkdir -p "$HERMES_DATA/profiles/.hidden"
printf '{"desired_state":"running"}' > "$HERMES_DATA/profiles/.hidden/gateway_state.json"
if gateway_guard; then exit 1; fi
''')

    def test_gateway_profile_parent_symlink_refused(self):
        self.shell(r'''
mkdir "$HERMES_DATA" "$SCRATCH/elsewhere"
ln -s "$SCRATCH/elsewhere" "$HERMES_DATA/profiles"
if gateway_guard; then exit 1; fi
''')

    def test_capture_final_output_limit(self):
        self.shell(r'''
# Simulate a child that finishes before the first polling check.
kill() { [[ "$1" == -0 ]] && return 1; builtin kill "$@"; }
run_capture 10 /bin/dd if=/dev/zero bs=1048576 count=9
[[ $? == 3 && -z "$CHILD_PID" ]]
''')


    def test_http_malformed_is_not_offline(self):
        self.shell(r'''
printf 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n' > "$SCRATCH/headers"
validate_http_headers "$SCRATCH/headers" || exit
printf 'HTTP/1.1 503 Unavailable\r\n\r\n' > "$SCRATCH/headers"
validate_http_headers "$SCRATCH/headers"
[[ $? == 3 ]] || exit 1
printf 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Type: text/plain\r\n\r\n' > "$SCRATCH/headers"
validate_http_headers "$SCRATCH/headers"
[[ $? == 4 ]] || exit 1
printf 'HTTP/1.1 302 Found\r\nLocation: https://elsewhere.invalid/\r\n\r\n' > "$SCRATCH/headers"
validate_http_headers "$SCRATCH/headers"
[[ $? == 4 ]]
''')

    def test_residual_cid_without_labels_refused(self):
        self.shell(r'''
RUN_CID=$SCRATCH/cid
printf '%064d\n' 0 > "$RUN_CID"
inventory_snapshot() { json -n '[{id:("0"*64),state:"exited",mounts:[],labels:{}}]'; }
if inventory_guard; then exit 1; fi
inventory_snapshot() { printf '[]\n'; }
inventory_guard || exit
printf 'malformed\n' > "$RUN_CID"
if inventory_guard; then exit 1; fi
''')


    def test_replaced_home_or_docs_refused(self):
        for name in ('HERMES_DATA', 'DOCUMENTS', 'USER_DOCUMENTS'):
            with self.subTest(directory=name):
                self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
path=$DIRECTORY_PLACEHOLDER
mv "$path" "$path.saved"
mkdir -m 700 "$path"
if verify_lock; then exit 1; fi
'''.replace('DIRECTORY_PLACEHOLDER',name))

    def test_os_architecture_version_matrix(self):
        self.shell(r'''
platform_rules Darwin arm64 27.0 || exit
[[ $NATIVE == arm64 && $REQUIRED_PODMAN == 6.1.1 ]] || exit 1
platform_rules Darwin arm64 28.1 || exit
platform_rules Darwin arm64 027.0 || exit
if platform_rules Darwin arm64 9999999999999999999999; then exit 1; fi
if platform_rules Darwin arm64 27.preview; then exit 1; fi
if platform_rules Darwin arm64 26.9; then exit 1; fi
if platform_rules Darwin arm64 15.7; then exit 1; fi
platform_rules Darwin x86_64 15.7 || exit
[[ $NATIVE == amd64 && $REQUIRED_PODMAN == 5.8.3 ]] || exit 1
version_at_least 5.8.3 "$REQUIRED_PODMAN" || exit
version_at_least 5.8.4 "$REQUIRED_PODMAN" || exit
if version_at_least 5.8.2 "$REQUIRED_PODMAN"; then exit 1; fi
platform_rules Linux x86_64 || exit
[[ $NATIVE == amd64 && $REQUIRED_PODMAN == 6.1.1 ]] || exit 1
if platform_rules Linux aarch64; then exit 1; fi
if platform_rules Darwin arm64 14.7; then exit 1; fi
if platform_rules Darwin x86_64 14.7; then exit 1; fi
if platform_rules Windows x86_64; then exit 1; fi
''')


    def test_macos_readiness_requires_integer_resources(self):
        self.shell(r'''
HOST_OS=Darwin; NATIVE=arm64; REQUIRED_PODMAN=6.1.1
BOUND_MACHINE=fixture
test_graph=/var/home/core/.local/share/containers/storage
test_vm_workspace_visible=true
resources='{"CPUs":2,"Memory":6144}'
podman_json() {
 case "$*" in
  'version --format json') printf '{"Client":{"Version":"6.1.1"}}' ;;
  'info --format json') printf '{"version":{"Version":"6.1.1"},"host":{"os":"linux","arch":"arm64","cgroupVersion":"v2","security":{"rootless":true},"cpus":2,"memTotal":6442450944},"store":{"graphRoot":"/home/user/storage"}}' | json --arg graph "$test_graph" '.store.graphRoot=$graph' ;;
  'machine info --format json') printf '{"Host":{"DefaultMachine":"fixture","MachineImageDir":"/fixture"}}' ;;
  'machine inspect fixture') json -n --argjson resources "$resources" '[{Name:"fixture",State:"running",Rootful:false,Resources:$resources}]' ;;
  *) return 99 ;;
 esac
}
run_capture() {
 if [[ "$6" == "test -d "* ]]; then
  # Execute the exact remote-shell command locally against a real test path.
  $test_vm_workspace_visible && /bin/sh -c "$6"; return
 fi
 guest_checked=true
 CAPTURE_OUT=$SCRATCH/df
 printf 'Filesystem 1024-blocks Used Available Capacity Mounted\nfixture 99999999 0 99999999 0%% /\n' > "$CAPTURE_OUT"
}
free_bytes() { printf '99999999999\n'; }
image_cached() { return 0; }
host_readiness digest || exit
original_workspace=$WORKSPACE
WORKSPACE="$ACCOUNT_HOME/Space and ' quote \$(touch SHOULD_NOT_EXIST)"
mkdir "$WORKSPACE" || exit
(cd "$SCRATCH" && host_readiness digest) || exit
[[ ! -e "$SCRATCH/SHOULD_NOT_EXIST" ]] || exit 1
WORKSPACE=$original_workspace
test_vm_workspace_visible=false
if host_readiness digest; then exit 1; fi
test_vm_workspace_visible=true
for test_graph in '/var/home/core/.local/share/containers/storage' '/home/user/storage'; do
 host_readiness digest || exit
done
for test_graph in '/home/user/space here' '/home/user/$(touch bad)' '/home/user/x;id'; do
 guest_checked=false
 if host_readiness digest; then exit 1; fi
 [[ "$guest_checked" == false ]] || exit 1
done
test_graph=/var/home/core/.local/share/containers/storage
for field in CPUs Memory; do
 for value in '"unknown"' null true '{}' '[]' 6144.5; do
  resources=$(json -n --arg field "$field" --argjson value "$value" '{CPUs:2,Memory:6144} | .[$field]=$value')
  if host_readiness digest; then exit 1; fi
 done
done
''')

    def test_linux_native_readiness(self):
        self.shell(r'''
HOST_OS=Linux; NATIVE=amd64; REQUIRED_PODMAN=6.1.1
client=6.1.1; server=6.1.1; rootless=true; remote=false; free=20000000000
safe_path() { return 0; }
free_bytes() { printf '%s\n' "$free"; }
image_cached() { return 0; }
podman_json() {
 case "$1" in
 version) json -n --arg v "$client" '{Client:{Version:$v}}' ;;
 info) json -n --arg v "$server" --argjson rootless "$rootless" --argjson remote "$remote" '{version:{Version:$v},host:{os:"linux",arch:"amd64",cgroupVersion:"v2",security:{rootless:$rootless},cpus:4,memTotal:8589934592,serviceIsRemote:$remote},store:{graphRoot:"/home/test/.local/share/containers/storage"}}' ;;
 *) exit 99 ;;
 esac
}
host_readiness digest || exit
client=6.0.2
if host_readiness digest; then exit 1; fi
client=6.2.0; server=6.3.0
host_readiness "sha256:test" || exit
client=6.1.1; server=5.8.1
if host_readiness digest; then exit 1; fi
server=6.1.1; rootless=false
if host_readiness digest; then exit 1; fi
rootless=true; remote=true
if host_readiness digest; then exit 1; fi
remote=false; free=1
if host_readiness digest; then exit 1; fi
''')

    def test_linux_runtime_identity_and_mounts(self):
        self.shell('''
HOST_OS=Linux; NATIVE=amd64
build_runtime '%s' chat "$HERMES_DATA" || exit
printf '%%s\\n' "${RUNTIME[@]}" > "$SCRATCH/args"
grep -Fx -- '--userns=keep-id' "$SCRATCH/args" || exit
grep -Fx -- '--user=0:0' "$SCRATCH/args" || exit
[[ $(grep -c 'relabel=private' "$SCRATCH/args") == 2 ]] || exit 1
[[ $(grep -c 'relabel=shared' "$SCRATCH/args") == 2 ]]
''' % json.dumps(dict(IMAGE,platform='amd64',digest=LEGACY_IMAGE['amd64'])))

    def test_linux_local_engine_options(self):
        self.shell(r'''
HOST_OS=Linux
safe_path() { [[ $1 == /run/user/$OPERATOR_UID && $2 == directory && $3 == 700 ]]; }
child_environment || exit
[[ ${PODMAN_OPTIONS[0]} == --remote=false ]] || exit 1
printf '%s\n' "${CHILD_ENV[@]}" | grep -Fx "XDG_RUNTIME_DIR=/run/user/$OPERATOR_UID"
''')

    def test_same_docs_mount_refused(self):
        self.shell(r'''
inventory_snapshot() { json -n --arg docs "$DOCUMENTS" '[{id:("0"*64),state:"running",labels:{},mounts:[{source:$docs,target:"/data"}]}]'; }
inventory_guard; [[ $? == 4 ]]
''')


    def test_vm_share_checks_fresh_content_and_cleans_markers(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
# Exercise the SSH shell boundary with spaces, apostrophes and shell punctuation.
USER_DOCUMENTS="$USER_DOCUMENTS/quotes' and \$(touch BAD)"
USER_DOCS_SOURCE=$USER_DOCUMENTS
mkdir -m 700 "$USER_DOCUMENTS" || exit
pin_source_path "$USER_DOCUMENTS" || exit
checks=0
run_capture() {
 checks=$((checks+1))
 [[ "$3 $4 $5" == 'machine ssh fixture' ]] || return 99
 /bin/sh -c "$6"
}
# No VM is contacted. Only the SSH dispatch is replaced by a local shell.
original_os=$HOST_OS
HOST_OS=Darwin; MACHINE=fixture; PODMAN=podman
file_stat() {
 if [[ "$original_os" == Linux ]]; then
  case "$1" in identity) stat -c %d:%i "$2" ;; owner) stat -c %u "$2" ;; mode) stat -c %a "$2" ;; esac
 else
  case "$1" in identity) stat -f %d:%i "$2" ;; owner) stat -f %u "$2" ;; mode) stat -f %Lp "$2" ;; esac
 fi
}
verify_vm_shares || exit
[[ "$checks" == 4 && ! -e BAD ]] || exit 1
[[ -z $(find "$WORKSPACE" "$USER_HOME/Documents" -name '.hermes-share.*' -print) ]] || exit 1
# The directory exists on the host, but a different VM directory has no marker.
run_capture() { return 1; }
verify_vm_shares; status=$?
[[ "$status" == 3 ]] || exit 1
[[ -z $(find "$WORKSPACE" "$USER_HOME/Documents" -name '.hermes-share.*' -print) ]]
''')


    def test_wrong_http_content_type_is_not_offline(self):
        result = self.shell(r'''
http_get() { HTTP_TYPE=text/html; HTTP_BODY='{}'; HTTP_LINK=; }
github_releases; [[ $? == 4 ]] || exit 1
registry_children v2026.7.20 unused; [[ $? == 4 ]] || exit 1
github_releases() { printf '{"tag":"v2026.7.20"}'; }
resolve_online null; [[ $? == 4 ]] || exit 1
image_cached() { exit 99; }
choose_image 'IMAGE_PLACEHOLDER'; [[ $? == 4 ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))
        self.assertIn('Release check returned invalid or inconsistent metadata', result.stderr)


    def test_cache_only_failure_is_explicit_and_never_pulls(self):
        result = self.shell(r'''
image_cached() { return "$cache_status"; }
run_capture() { printf unexpected-pull > "$SCRATCH/pulled"; return 0; }
for cache_status in 0 1 3 143; do
 expected=$cache_status
 [[ "$cache_status" != 1 ]] || expected=3
 require_cached_image 'sha256:test'; [[ $? == "$expected" ]] || exit 1
 ensure_image 'sha256:test' false; [[ $? == "$expected" ]] || exit 1
done
[[ ! -e "$SCRATCH/pulled" ]]
''')
        self.assertIn('required image is not cached; no pull was attempted:', result.stderr)

    def test_image_cache_preserves_cancellation(self):
        result = self.shell(r'''
podman_status() { INTERRUPTED=15; return 143; }
image_cached 'sha256:test'; [[ $? == 143 ]]
''')
        self.assertNotIn('image cache inspection failed', result.stderr)

    def test_pull_failure_and_missing_result_are_explicit(self):
        result = self.shell(r'''
PODMAN=unused
verify_lock() { return 0; }
image_cached() { return 1; }
run_capture() { return "$pull_status"; }
for pull_status in 125 3 0; do
 expected=$pull_status
 [[ "$pull_status" != 0 ]] || expected=3
 ensure_image 'sha256:test' true; [[ $? == "$expected" ]] || exit 1
done
run_capture() { INTERRUPTED=2; return 130; }
ensure_image 'sha256:test' true; [[ $? == 130 ]]
''')
        self.assertIn('image pull failed (status 125)', result.stderr)
        self.assertIn('image pull failed (status 3)', result.stderr)
        self.assertIn('required image is not cached', result.stderr)
        self.assertNotIn('no pull was attempted', result.stderr)
        self.assertNotIn('image pull failed (status 130)', result.stderr)


    def test_minimum_layout_and_case_insensitive_selection(self):
        self.shell(r'''
workspace_base() { printf '%s\n' "$ACCOUNT_HOME"; }
for spelling in workspace Workspace WORKSPACE wOrKsPaCe; do
 WORKSPACE_ARGUMENT=$spelling
 select_workspace || exit
 [[ "$WORKSPACE_ARGUMENT" == Workspace && "$WORKSPACE" == "$ACCOUNT_HOME/Workspace" ]] || exit 1
done
load_workspace && acquire_lock && prepare_directories || exit
save_image 'IMAGE_PLACEHOLDER' || exit
[[ "$IMAGE_FILE" == "$ACCOUNT_HOME/.config/hermes/Workspace.json" ]] || exit 1
[[ $(read_image) == "$IMAGE_CONFIG" && ! -e "$METADATA" ]] || exit 1
[[ $(printf '%s' "$IMAGE_CONFIG" | json 'keys|length') == 3 ]] || exit 1
[[ "$ACTIVE_LOCK" == "$RUNTIME_BASE/hermes/$WORKSPACE_HASH.lock" ]] || exit 1
release_lock || exit
[[ ! -e "$ACTIVE_LOCK" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_pretty_image_config_and_unknown_fields(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
save_image 'IMAGE_PLACEHOLDER' || exit
"$JQ" . "$IMAGE_FILE" > "$SCRATCH/pretty"
cat "$SCRATCH/pretty" > "$IMAGE_FILE"
load_workspace || exit
printf '%s' "$IMAGE_CONFIG" | json '.extra=true' > "$IMAGE_FILE"
if load_workspace; then exit 1; fi
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_config_divergence_and_parent_replacement_are_preserved(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
save_image 'IMAGE_PLACEHOLDER' || exit
printf '%s' "$IMAGE_CONFIG" | json '.tag="v2026.7.21"' > "$IMAGE_FILE"
if save_image "$IMAGE_CONFIG"; then exit 1; fi
[[ $(read_image | json -r .tag) == v2026.7.21 ]] || exit 1
mv "$CONFIG_DIR" "$CONFIG_DIR.saved"
mkdir -m 700 "$CONFIG_DIR"
if verify_lock; then exit 1; fi
[[ -f "$CONFIG_DIR.saved/Workspace.json" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_runtime_lock_is_exclusive_and_replacement_is_preserved(self):
        self.shell(r'''
load_workspace && acquire_lock || exit
if (LOCK_HELD=false; acquire_lock); then exit 1; fi
mv "$ACTIVE_LOCK" "$ACTIVE_LOCK.saved"
mkdir -m 700 "$ACTIVE_LOCK"
printf foreign > "$ACTIVE_LOCK/token"
if verify_lock; then exit 1; fi
release_lock || exit
[[ $(cat "$ACTIVE_LOCK/token") == foreign ]]
''')

    def test_config_and_runtime_paths_refuse_unsafe_locations(self):
        self.shell(r'''
XDG_CONFIG_HOME=relative
if set_paths "$WORKSPACE"; then exit 1; fi
XDG_CONFIG_HOME=$ACCOUNT_HOME/config-link
ln -s "$SCRATCH" "$XDG_CONFIG_HOME"
set_paths "$WORKSPACE" || exit
if load_workspace; then exit 1; fi
unset XDG_CONFIG_HOME
set_paths "$WORKSPACE" && load_workspace || exit
chmod 777 "$RUNTIME_BASE"
if acquire_lock; then exit 1; fi
[[ ! -e "$ACTIVE_LOCK" ]]
''')

    def legacy_script(self):
        return r'''
mkdir -p "$METADATA/bash" "$METADATA/locks/operation.lock"
json -n --arg workspace "$WORKSPACE" --argjson image 'LEGACY_IMAGE_PLACEHOLDER' '{schema:1,owner:"bash",workspace:$workspace,generation:3,sequence:0,accepted:null,pending:{id:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",action:"setup",before:null,target:$image,phase:"run-intent",attempt:1,backup:null,sequence:1}}' > "$LEGACY_STATE"
legacy_hash=$(file_hash "$LEGACY_STATE")
'''.replace('LEGACY_IMAGE_PLACEHOLDER', json.dumps(LEGACY_IMAGE))

    def operation_script(self):
        return r'''
image=$(printf '%s' 'IMAGE_PLACEHOLDER' | json .)
select_workspace() { return 0; }
inventory_guard() { return 0; }
gateway_guard() { return 0; }
host_readiness() { return 0; }
verify_vm_shares() { return 0; }
ensure_image() { return 0; }
confirm() { return 0; }
choose_setup_action() { return 0; }
filevault_prompt() { return 0; }
choose_image() { SELECTED=$image; ALLOW_PULL=true; }
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE))

    def test_legacy_adoption_requires_confirmation_and_preserves_records(self):
        self.shell(self.legacy_script()+self.operation_script()+r'''
confirm() { return 130; }
operate; [[ $? == 130 && ! -e "$IMAGE_FILE" ]] || exit 1
confirm() { return 0; }
choose_image() { exit 99; }
create_backup() { return 0; }
run_session() { [[ $(read_image) == "$IMAGE_CONFIG" ]] || exit 99; return 1; }
operate; [[ $? == 1 ]] || exit 1
[[ $(read_image) == "$image" && $(file_hash "$LEGACY_STATE") == "$legacy_hash" ]]
''')

    def test_legacy_active_lock_blocks_new_launcher(self):
        self.shell(self.legacy_script()+r'''
mkdir "$METADATA/locks/operation.lock/active"
if load_workspace; then exit 1; fi
[[ $(file_hash "$LEGACY_STATE") == "$legacy_hash" && ! -e "$CONFIG_DIR" ]]
''')

    def test_fresh_setup_and_chat_keep_only_image_configuration(self):
        self.shell(self.operation_script()+r'''
run_session() { [[ $(read_image) == "$image" && ! -e "$METADATA" ]]; }
operate || exit
release_lock || exit
ACTION=chat
choose_image() { printf checked > "$SCRATCH/checked"; SELECTED=$image; ALLOW_PULL=true; }
create_backup() { exit 99; }
operate || exit
release_lock || exit
[[ $(cat "$SCRATCH/checked") == checked && ! -e "$METADATA" ]]
''')

    def test_failed_setup_keeps_desired_image_without_repair_state(self):
        self.shell(self.operation_script()+r'''
run_session() { printf fixture > "$HERMES_DATA/config.yaml"; return 1; }
operate; [[ $? == 1 ]] || exit 1
release_lock || exit
[[ $(read_image) == "$image" && ! -e "$METADATA" ]] || exit 1
# There is no transaction to replay: the next launch uses the desired image.
ACTION=chat
load_workspace || exit
[[ "$MIGRATION_IMAGE" == null && "$IMAGE_CONFIG" == "$image" ]]
''')

    def test_failed_backup_prevents_image_change_or_execution(self):
        self.shell(self.operation_script()+r'''
load_workspace && acquire_lock && prepare_directories && save_image "$image" && release_lock || exit
before=$image
image=$(printf '%s' "$image" | json '.tag="v2026.7.21" | .digest="sha256:"+("c"*64)')
create_backup() { return 4; }
run_session() { exit 99; }
operate; [[ $? == 4 ]] || exit 1
[[ $(read_image) == "$before" ]]
''')

    def native_fixture_script(self):
        return r'''
load_workspace && acquire_lock && prepare_directories || exit
inventory_guard() { return 0; }
gateway_guard() { return 0; }
check_backup_sources() { return 0; }
native_backup() {
 cp "$ACCOUNT_HOME/fixture.zip" "$2/hermes-backup-2026-09-12-000000.zip" || return
 CAPTURE_OUT=$SCRATCH/native.out; CAPTURE_ERR=$SCRATCH/native.err
 printf 'Backup complete: /opt/data/backups/%s/hermes-backup-2026-09-12-000000.zip\n' "${2#"$BACKUPS/"}" > "$CAPTURE_OUT"
 printf '%s' "${injected_warning:-}" > "$CAPTURE_ERR"
}
'''

    def test_backup_published_with_minimum_receipt_and_no_success_logs(self):
        self.shell(self.native_fixture_script()+r'''
create_backup 'IMAGE_PLACEHOLDER' || exit
final=$BACKUPS
receipt=$(read_record "$final/hermes-backup-2026-09-12-000000-receipt.json") || exit
validate_receipt "$receipt" || exit
[[ $(printf '%s' "$receipt" | json '.schema') == 2 ]] || exit 1
[[ $(printf '%s' "$receipt" | json 'keys|length') == 7 ]] || exit 1
[[ -f "$final/hermes-backup-2026-09-12-000000.zip" && ! -e "$BACKUPS/.pending-$OPERATION_ID" && ! -e "$METADATA" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)), setup=self.zip_fixture)

    def test_flat_backup_retention_and_orphan_preservation(self):
        self.shell(self.native_fixture_script()+r'''
create_backup 'IMAGE_PLACEHOLDER' || exit
file=$BACKUPS/hermes-backup-2026-09-12-000000-receipt.json
receipt=$(read_record "$file" | json '.captured="2020-01-01T00:00:00Z"') || exit
atomic_record "$file" "$receipt" || exit
printf preserve > "$BACKUPS/hermes-backup-2020-01-02-000000.zip"
retain_backups || exit
[[ -d "$BACKUPS" && ! -e "$file" && ! -e "$BACKUPS/hermes-backup-2026-09-12-000000.zip" ]] || exit 1
[[ $(cat "$BACKUPS/hermes-backup-2020-01-02-000000.zip") == preserve ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)), setup=self.zip_fixture)

    def test_backup_name_collisions_preserve_existing_files(self):
        for suffix in ('.zip', '-receipt.json'):
            self.shell(self.native_fixture_script()+r'''
existing=$BACKUPS/hermes-backup-2026-09-12-000000SUFFIX
printf preserve > "$existing"
create_backup 'IMAGE_PLACEHOLDER'; [[ $? == 4 ]] || exit 1
[[ $(cat "$existing") == preserve && -f "$BACKUPS/.pending-$OPERATION_ID/receipt.json" ]]
'''.replace('SUFFIX', suffix).replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)), setup=self.zip_fixture)

    def test_failed_backup_logs_preserved_and_not_published(self):
        self.shell(self.native_fixture_script()+r'''
injected_warning='Warning: files skipped'
create_backup 'IMAGE_PLACEHOLDER'; [[ $? == 4 ]] || exit 1
[[ -f "$BACKUPS/.pending-$OPERATION_ID/stderr" && ! -e "$BACKUPS/hermes-backup-2026-09-12-000000.zip" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)), setup=self.zip_fixture)


    def test_cancelled_config_publication_does_not_select_an_image(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
checks=0
verify_lock() { checks=$((checks+1)); [[ "$checks" != 2 ]] || INTERRUPTED=15; return 0; }
save_image 'IMAGE_PLACEHOLDER'; [[ $? == 143 ]] || exit 1
[[ ! -e "$IMAGE_FILE" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_release_metadata_remains_strict_without_storing_it(self):
        self.shell(r'''
http_get() { HTTP_TYPE=application/json; HTTP_LINK=; HTTP_BODY=$response; }
for response in '[{"id":1.5,"draft":false,"prerelease":false,"tag_name":"v2026.7.20","published_at":"2026-07-20T00:00:00Z"}]' '[{"id":1,"draft":false,"prerelease":false,"tag_name":"v2026.7.20","published_at":"2026-02-30T00:00:00Z"}]'; do
 github_releases; [[ $? == 4 ]] || exit 1
done
''')

    def test_linux_respects_custom_xdg_runtime(self):
        self.shell(r'''
HOST_OS=Linux
XDG_RUNTIME_DIR=/run/user/custom
safe_path() { [[ "$1" == "$XDG_RUNTIME_DIR" && "$2" == directory && "$3" == 700 ]]; }
child_environment || exit
[[ "$RUNTIME_DIR" == "$XDG_RUNTIME_DIR" ]] || exit 1
printf '%s\n' "${CHILD_ENV[@]}" | grep -Fx "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" || exit
XDG_RUNTIME_DIR=relative
if child_environment; then exit 1; fi
''')

    def test_import_arguments(self):
        self.shell(r'''
parse_args restore || exit
[[ $ACTION == restore && -z $WORKSPACE_ARGUMENT && -z $IMPORT_ARGUMENT ]] || exit 1
parse_args restore EZIRIUS || exit
[[ $WORKSPACE_ARGUMENT == EZIRIUS && -z $IMPORT_ARGUMENT ]] || exit 1
parse_args restore ezirius '/tmp/a b.zip' || exit
[[ $WORKSPACE_ARGUMENT == ezirius && $IMPORT_ARGUMENT == '/tmp/a b.zip' ]] || exit 1
parse_args restore './a b.zip' || exit
[[ -z $WORKSPACE_ARGUMENT && $IMPORT_ARGUMENT == './a b.zip' ]] || exit 1
if parse_args restore x y z; then exit 1; fi
if parse_args chat x y; then exit 1; fi
if parse_args restore x ''; then exit 1; fi
''')

    def test_import_picker_orders_newest_and_reads_legacy(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
mkdir "$BACKUPS/$OPERATION_ID" "$BACKUPS/.pending-example"
: > "$BACKUPS/hermes-backup-2026-09-12-000000.zip"
: > "$BACKUPS/$OPERATION_ID/hermes-backup-2026-09-13-000000.zip"
: > "$BACKUPS/.pending-example/hermes-backup-2026-09-14-000000.zip"
IMPORT_ARGUMENT=
select_import_backup <<< 1 || exit
[[ $IMPORT_ARCHIVE == "$BACKUPS/$OPERATION_ID/hermes-backup-2026-09-13-000000.zip" ]] || exit 1
select_import_backup <<< 2 || exit
[[ $IMPORT_ARCHIVE == "$BACKUPS/hermes-backup-2026-09-12-000000.zip" ]] || exit 1
select_import_backup <<< Q; [[ $? == 130 ]] || exit 1
select_import_backup <<< $'3\n1' || exit
[[ $IMPORT_ARCHIVE == "$BACKUPS/$OPERATION_ID/hermes-backup-2026-09-13-000000.zip" ]] || exit 1
select_import_backup <<< q; [[ $? == 130 ]] || exit 1
select_import_backup < /dev/null; [[ $? == 130 ]]
''')

    def test_import_empty_list_and_unconfigured_home_with_backups(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
ACTION=restore; IMPORT_ARGUMENT=
select_import_backup; [[ $? == 4 ]] || exit 1
printf preserve > "$BACKUPS/existing.zip"
load_workspace || exit
printf unmanaged > "$HERMES_DATA/config.yaml"
if load_workspace; then exit 1; fi
''')

    def import_fixture_script(self):
        return r'''
load_workspace && acquire_lock && prepare_directories || exit
image='IMAGE_PLACEHOLDER'
save_image "$image" && release_lock || exit
printf 'old config' > "$HERMES_DATA/config.yaml"
ACTION=restore; IMPORT_ARGUMENT=$ACCOUNT_HOME/fixture.zip
select_workspace() { return 0; }
host_readiness() { return 0; }
verify_vm_shares() { return 0; }
ensure_image() { return 0; }
inventory_guard() { return 0; }
filevault_prompt() { return 0; }
confirm() {
 printf '%s\n' "$1" >> "$SCRATCH/prompts"
 [[ "$1" != "$cancel_prompt" ]]
}
cancel_prompt=
staged_calls=0; live_calls=0; backup_calls=0
native_import() {
 if [[ $2 == staged ]]; then
  staged_calls=$((staged_calls+1))
  [[ ${fail_staged:-false} != true ]] || return 4
 else
  live_calls=$((live_calls+1))
  [[ $backup_calls == 1 ]] || return 4
  [[ ${fail_live:-false} != true ]] || return 4
 fi
 /usr/bin/unzip -qo "$IMPORT_STAGE/input.zip" -d "$1"
}
create_backup() { backup_calls=$((backup_calls+1)); }
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE))

    def test_import_stages_then_backs_up_then_restores(self):
        self.shell(self.import_fixture_script()+r'''
operate_import || exit
[[ $staged_calls == 1 && $backup_calls == 1 && $live_calls == 1 ]] || exit 1
[[ $(cat "$HERMES_DATA/config.yaml") == 'model: fixture' ]] || exit 1
[[ -f "$IMPORT_ARGUMENT" && ! -e "$IMPORT_STAGE" ]] || exit 1
release_lock || exit
[[ ! -e "$ACTIVE_LOCK" ]]
''', setup=self.zip_fixture)

    def test_import_refuses_hidden_control_characters_before_live_writes(self):
        def setup(root):
            self.zip_fixture(root)
            with zipfile.ZipFile(root/'fixture.zip', 'a') as archive:
                archive.writestr('bad\nname', 'data')
            restored = root/'restored'
            restored.mkdir(mode=0o700)
            (restored/'config.yaml').write_text('model: fixture')
            # Native Python import preserves this name; unzip listings show ^J.
            (restored/'bad\nname').write_text('data')
        result = self.shell(self.import_fixture_script()+r'''
native_import() {
 [[ $2 == staged ]] || { live_calls=$((live_calls+1)); return 99; }
 staged_calls=$((staged_calls+1))
 cp -R "$ACCOUNT_HOME/restored/." "$1"
}
operate_import; status=$?
[[ $status == 4 && $staged_calls == 1 && $live_calls == 0 && $backup_calls == 0 ]] || exit 1
[[ $(cat "$HERMES_DATA/config.yaml") == 'old config' && -d "$IMPORT_STAGE" ]]
''', setup=setup)
        self.assertIn('control character in a Hermes Home filename', result.stderr)

    def test_home_entry_names_allow_unicode_but_reject_controls(self):
        for name, status in [('Ελληνικά.txt', 0), ('notes with spaces.txt', 0),
                             ('bad\tname', 4), ('bad\rname', 4),
                             ('bad\x1bname', 4), ('bad\u202ename', 4)]:
            with self.subTest(name=name):
                def setup(root):
                    (root/'restored').mkdir(mode=0o700)
                    (root/'restored'/name).write_text('data')
                self.shell('check_home_entries "$ACCOUNT_HOME/restored"',
                           expect=status, setup=setup)

    def test_backup_exclusion_treats_home_path_literally(self):
        def setup(root):
            home = root/'Home [copy]*?'
            (home/'backups').mkdir(parents=True, mode=0o700)
            (home/'config.yaml').write_text('model: fixture')
            # An existing archive is not input to the next backup.
            with (home/'backups'/'old.zip').open('wb') as archive:
                archive.truncate(2 * 1024**3)
            (home/'backups'/'old\nname').write_text('excluded')
            (home/'nested'/'backups').mkdir(parents=True)
            (home/'nested'/'backups'/'bad\nname').write_text('must be checked')
        self.shell(r'''
HERMES_DATA="$ACCOUNT_HOME/Home [copy]*?"
BACKUPS=$HERMES_DATA/backups
free_bytes() { printf '1100000000\n'; }
# Skip only Home/backups, not another directory with the same name.
check_home_entries "$HERMES_DATA"; [[ $? == 4 ]] || exit 1
rm "$HERMES_DATA/nested/backups/"*
check_backup_sources || exit
''', setup=setup)

    def test_failed_staged_import_does_not_touch_live_home(self):
        self.shell(self.import_fixture_script()+r'''
fail_staged=true
operate_import; [[ $? == 4 ]] || exit 1
[[ $backup_calls == 0 && $live_calls == 0 && -f "$IMPORT_STAGE/input.zip" ]] || exit 1
[[ $(cat "$HERMES_DATA/config.yaml") == 'old config' ]]
''', setup=self.zip_fixture)

    def test_cancel_import_before_live_write(self):
        self.shell(self.import_fixture_script()+r'''
cancel_prompt='Apply this backup to Hermes Home? Matching files will be overwritten; other files remain.'
if operate_import; then exit 1; fi
[[ $staged_calls == 1 && $backup_calls == 0 && $live_calls == 0 ]] || exit 1
[[ $(cat "$HERMES_DATA/config.yaml") == 'old config' ]]
''', setup=self.zip_fixture)

    def test_live_import_failure_preserves_staging(self):
        self.shell(self.import_fixture_script()+r'''
fail_live=true
operate_import; [[ $? == 4 ]] || exit 1
[[ $backup_calls == 1 && $live_calls == 1 && -f "$IMPORT_STAGE/input.zip" ]] || exit 1
[[ $(read_image) == "$IMAGE_CONFIG" ]]
''', setup=self.zip_fixture)

    def test_import_runtime_mounts_readonly_archive_without_docs(self):
        self.shell(r'''
build_runtime 'IMAGE_PLACEHOLDER' import "$HERMES_DATA" "$BACKUPS/a b.zip" || exit
printf '%s\n' "${RUNTIME[@]}" > "$SCRATCH/args"
grep -Fx -- '--network=none' "$SCRATCH/args" || exit
grep -Fx -- "$(mount_value "$BACKUPS/a b.zip" /import.zip shared ro)" "$SCRATCH/args" || exit
[[ $(grep -c '^--mount$' "$SCRATCH/args") == 3 ]] || exit 1
grep -Fx -- "$(mount_value "$BACKUPS" /opt/data/backups)" "$SCRATCH/args" || exit
[[ ${RUNTIME[${#RUNTIME[@]}-3]} == import && ${RUNTIME[${#RUNTIME[@]}-2]} == /import.zip && ${RUNTIME[${#RUNTIME[@]}-1]} == --force ]] || exit 1
build_runtime 'IMAGE_PLACEHOLDER' import "$BACKUPS/.import-test/home" "$BACKUPS/a b.zip" || exit
printf '%s\n' "${RUNTIME[@]}" > "$SCRATCH/args"
[[ $(grep -c '^--mount$' "$SCRATCH/args") == 2 ]] || exit 1
if grep -Fx -- "$(mount_value "$BACKUPS" /opt/data/backups)" "$SCRATCH/args"; then exit 1; fi
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_import_receipt_mismatch_blocks_staging(self):
        self.shell(self.import_fixture_script()+r'''
receipt=$(json -n --arg workspace "$WORKSPACE" --arg operation "$OPERATION_ID" --argjson source "$image" '{schema:2,workspace:$workspace,operation:$operation,name:"hermes-backup-2026-09-12-000000.zip",hash:("0"*64),captured:"2026-09-12T00:00:00Z",source:$source}')
printf '%s' "$receipt" > "$ACCOUNT_HOME/fixture-receipt.json"
operate_import; [[ $? == 4 ]] || exit 1
[[ $staged_calls == 0 && $backup_calls == 0 && $live_calls == 0 ]]
''', setup=self.zip_fixture)

    def test_import_checks_restored_config_not_seeded_defaults(self):
        self.shell(self.import_fixture_script()+r'''
native_import() { printf 'seeded default' > "$1/config.yaml"; }
operate_import; [[ $? == 4 ]] || exit 1
[[ $backup_calls == 0 && $(cat "$HERMES_DATA/config.yaml") == 'old config' ]]
''', setup=self.zip_fixture)

    def test_import_backup_failure_blocks_live_restore(self):
        self.shell(self.import_fixture_script()+r'''
create_backup() { return 4; }
operate_import; [[ $? == 4 ]] || exit 1
[[ $live_calls == 0 && $(cat "$HERMES_DATA/config.yaml") == 'old config' ]]
''', setup=self.zip_fixture)

    def test_import_into_empty_home_saves_selected_image(self):
        self.shell(self.import_fixture_script()+r'''
rm "$IMAGE_FILE" "$HERMES_DATA/config.yaml"
choose_image() { SELECTED=$image; ALLOW_PULL=true; }
native_import() { /usr/bin/unzip -qo "$IMPORT_STAGE/input.zip" -d "$1"; }
operate_import || exit
[[ $backup_calls == 0 && $(cat "$HERMES_DATA/config.yaml") == 'model: fixture' ]] || exit 1
[[ $(read_image) == "$IMAGE_CONFIG" && $IMAGE_CONFIG != null ]]
''', setup=self.zip_fixture)

    def test_native_import_rejects_warning_despite_success_status(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories || exit
IMPORT_STAGE=$BACKUPS/.import-$OPERATION_ID
mkdir "$IMPORT_STAGE"
SELECTED='IMAGE_PLACEHOLDER'
verify_import_stage() { return 0; }
inventory_guard() { return 0; }
PODMAN=/bin/sh
build_runtime() { RUNTIME=(-c 'echo "Import complete: 2 files restored in 0.1s"; echo "Warnings (1 files skipped)" >&2'); }
native_import "$HERMES_DATA" staged; [[ $? == 4 ]] || exit 1
[[ $RESIDUAL_UNKNOWN == false && -s "$IMPORT_STAGE/staged.stderr" ]]
build_runtime() { RUNTIME=(-c 'echo "Import complete: 2 files restored in 0.1s"'); }
native_import "$HERMES_DATA" staged || exit
run_capture() { INTERRUPTED=15; return 143; }
native_import "$HERMES_DATA" staged; [[ $? == 143 && $RESIDUAL_UNKNOWN == true ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_import_seed_warning_requires_fresh_home_and_matching_config(self):
        self.shell(r'''
IMPORT_STAGE=$ACCOUNT_HOME
cp "$ACCOUNT_HOME/fixture.zip" "$IMPORT_STAGE/input.zip"
mkdir "$ACCOUNT_HOME/restored"
unzip -p "$IMPORT_STAGE/input.zip" config.yaml > "$ACCOUNT_HOME/restored/config.yaml"
printf 'Import complete: 1 files restored\n' > "$SCRATCH/import.out"
cat > "$SCRATCH/import.err" <<'WARNING'
[config-migrate] WARNING: This config predates version 12 (~2 years old) and can no longer be auto-migrated. Back up /opt/data/config.yaml and run `hermes setup` to regenerate, or manually set _config_version: 12 after reviewing the changelog.
WARNING
import_output_clean "$ACCOUNT_HOME/restored" true "$SCRATCH/import.out" "$SCRATCH/import.err" || exit
if import_output_clean "$ACCOUNT_HOME/restored" false "$SCRATCH/import.out" "$SCRATCH/import.err"; then exit 1; fi
cp "$SCRATCH/import.err" "$SCRATCH/extra.err"
printf 'Warning: file skipped\n' >> "$SCRATCH/extra.err"
if import_output_clean "$ACCOUNT_HOME/restored" true "$SCRATCH/import.out" "$SCRATCH/extra.err"; then exit 1; fi
printf mismatch > "$ACCOUNT_HOME/restored/config.yaml"
if import_output_clean "$ACCOUNT_HOME/restored" true "$SCRATCH/import.out" "$SCRATCH/import.err"; then exit 1; fi
# Backup verification never grants the import-only exception.
if native_output_clean "$SCRATCH/import.out" "$SCRATCH/import.err"; then exit 1; fi
''', setup=self.zip_fixture)

    def test_backup_command_arguments_and_dispatch(self):
        self.shell(r'''
parse_args backup || exit
[[ $ACTION == backup && -z $WORKSPACE_ARGUMENT ]] || exit 1
parse_args backup EZIRIUS || exit
[[ $WORKSPACE_ARGUMENT == EZIRIUS ]] || exit 1
if parse_args backup x y; then exit 1; fi
initialise() { return 0; }
operate_backup() { dispatched=true; }
operate() { return 99; }
main backup Workspace || exit
[[ $dispatched == true ]]
''')

    def explicit_backup_script(self):
        return self.native_fixture_script()+r'''
save_image 'IMAGE_PLACEHOLDER' && release_lock || exit
before=$(read_image)
ACTION=backup
select_workspace() { return 0; }
host_readiness() { return 0; }
verify_vm_shares() { return 0; }
ensure_image() { [[ "$2" == false ]]; }
filevault_prompt() { return 0; }
choose_image() { return 99; }
run_session() { return 99; }
pruned=false
retain_backups() { pruned=true; }
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE))

    def test_explicit_backup_preserves_config_and_prunes_after_success(self):
        self.shell(self.explicit_backup_script()+r'''
operate_backup || exit
[[ -f "$BACKUPS/hermes-backup-2026-09-12-000000.zip" && -f "$BACKUPS/hermes-backup-2026-09-12-000000-receipt.json" ]] || exit 1
[[ $pruned == true && $(read_image) == "$before" ]] || exit 1
release_lock || exit
[[ ! -e "$ACTIVE_LOCK" ]]
''', setup=self.zip_fixture)

    def test_failed_explicit_backup_does_not_prune(self):
        self.shell(self.explicit_backup_script()+r'''
injected_warning='Warning: files skipped'
operate_backup; [[ $? == 4 ]] || exit 1
[[ $pruned == false && $(read_image) == "$before" ]]
''', setup=self.zip_fixture)

    def test_explicit_backup_requires_config_and_respects_cancellation(self):
        self.shell(self.explicit_backup_script()+r'''
filevault_prompt() { return 130; }
operate_backup; [[ $? == 130 ]] || exit 1
[[ $pruned == false && ! -e "$BACKUPS/.pending-$OPERATION_ID" ]] || exit 1
release_lock || exit
rm "$IMAGE_FILE"
operate_backup; [[ $? == 4 ]]
''', setup=self.zip_fixture)

    def test_cancellation_during_backup_final_check_prevents_publication(self):
        self.shell(self.native_fixture_script()+r'''
verify_lock() {
 [[ ! -e "$BACKUPS/.pending-$OPERATION_ID/receipt.json" ]] || INTERRUPTED=15
 return 0
}
create_backup 'IMAGE_PLACEHOLDER'; [[ $? == 143 ]] || exit 1
[[ ! -e "$BACKUPS/hermes-backup-2026-09-12-000000.zip" && -e "$BACKUPS/.pending-$OPERATION_ID/receipt.json" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)), setup=self.zip_fixture)

    def test_cancellation_during_retention_final_check_preserves_archive(self):
        self.shell(self.native_fixture_script()+r'''
create_backup 'IMAGE_PLACEHOLDER' || exit
file=$BACKUPS/hermes-backup-2026-09-12-000000-receipt.json
receipt=$(read_record "$file" | json '.captured="2020-01-01T00:00:00Z"') || exit
atomic_record "$file" "$receipt" || exit
checks=0
verify_lock() { checks=$((checks+1)); [[ $checks != 2 ]] || INTERRUPTED=15; return 0; }
retain_backups; [[ $? == 143 ]] || exit 1
[[ -f "$file" && -f "$BACKUPS/hermes-backup-2026-09-12-000000.zip" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)), setup=self.zip_fixture)

    def test_cancellation_during_runtime_build_does_not_start_session(self):
        self.shell(r'''
verify_lock() { return 0; }
gateway_guard() { return 0; }
inventory_guard() { return 0; }
PODMAN=/bin/sh
build_runtime() { INTERRUPTED=15; RUNTIME=(-c 'exit 99'); return 0; }
run_session 'IMAGE_PLACEHOLDER' chat; [[ $? == 143 && -z $CHILD_PID && $RESIDUAL_UNKNOWN == false ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)))

    def test_import_receipt_version_must_not_exceed_selected_image(self):
        for tag, status in [('v2026.7.21', 4), ('v2026.7.19', 0)]:
            self.shell(self.import_fixture_script()+r'''
IMPORT_ARGUMENT=$ACCOUNT_HOME/hermes-backup-2026-09-12-000000.zip
cp "$ACCOUNT_HOME/fixture.zip" "$IMPORT_ARGUMENT"
hash=$(file_hash "$IMPORT_ARGUMENT")
receipt=$(json -n --arg workspace "$WORKSPACE" --arg operation "$OPERATION_ID" --arg hash "$hash" --argjson source "$image" '{schema:2,workspace:$workspace,operation:$operation,name:"hermes-backup-2026-09-12-000000.zip",hash:$hash,captured:"2026-09-12T00:00:00Z",source:($source|.tag="TAG_PLACEHOLDER")}')
printf '%s' "$receipt" > "${IMPORT_ARGUMENT%.zip}-receipt.json"
operate_import; [[ $? == STATUS_PLACEHOLDER ]] || exit 1
if [[ STATUS_PLACEHOLDER == 4 ]]; then
 [[ $staged_calls == 0 && $live_calls == 0 && $(cat "$HERMES_DATA/config.yaml") == 'old config' ]] || exit 1
fi
'''.replace('TAG_PLACEHOLDER', tag).replace('STATUS_PLACEHOLDER', str(status)), setup=self.zip_fixture)

    def test_import_capacity_checks_destination_volume(self):
        self.shell(r'''
IMPORT_STAGE=$BACKUPS/.import-test
run_capture() { CAPTURE_OUT=$SCRATCH/size; printf '1 file, 100 bytes uncompressed\n' > "$CAPTURE_OUT"; }
free_bytes() { printf '%s\n' "$1" > "$SCRATCH/checked-volume"; printf '9999999999\n'; }
check_import_capacity 2 || exit
[[ "$(cat "$SCRATCH/checked-volume")" == "$BACKUPS" ]] || exit 1
check_import_capacity 1 || exit
[[ "$(cat "$SCRATCH/checked-volume")" == "$HERMES_DATA" ]]
''')

    def test_import_rechecks_space_after_safety_backup(self):
        self.shell(self.import_fixture_script()+r'''
free_bytes() {
 if [[ $backup_calls == 0 ]]; then printf '99999999999\n'
 else printf '1\n'; fi
}
operate_import; [[ $? == 3 ]] || exit 1
[[ $staged_calls == 1 && $backup_calls == 1 && $live_calls == 0 ]] || exit 1
[[ $(cat "$HERMES_DATA/config.yaml") == 'old config' && -f "$IMPORT_STAGE/input.zip" ]]
''', setup=self.zip_fixture)

    def test_registry_descriptor_size_must_be_integer(self):
        self.shell(r'''
http_get() {
 HTTP_TYPE=application/vnd.oci.image.index.v1+json
 HTTP_BODY=$(json -n '{schemaVersion:2,manifests:[{digest:("sha256:"+("a"*64)),size:1.5,mediaType:"application/vnd.oci.image.manifest.v1+json",platform:{os:"linux",architecture:"arm64"}},{digest:("sha256:"+("b"*64)),size:1,mediaType:"application/vnd.oci.image.manifest.v1+json",platform:{os:"linux",architecture:"amd64"}}]}')
}
registry_children v2026.7.20 token; [[ $? == 4 ]]
''')

    def test_http_size_limits_never_allow_offline_fallback(self):
        self.shell(r'''
CURL=unused
run_capture() {
 CAPTURE_OUT=$SCRATCH/http.body; CAPTURE_ERR=$SCRATCH/http.error
 printf '{}' > "$CAPTURE_OUT"; : > "$CAPTURE_ERR"
 printf 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n' > "$headers"
 case "$scenario" in
  curl_limit) return 63 ;;
  body_limit) /bin/dd if=/dev/zero of="$CAPTURE_OUT" bs=8388609 count=1 2>/dev/null; return 3 ;;
  header_limit) /bin/dd if=/dev/zero of="$headers" bs=65537 count=1 2>/dev/null ;;
  timeout) return 28 ;;
  cancelled) INTERRUPTED=15; return 143 ;;
 esac
}
for scenario in curl_limit body_limit header_limit timeout cancelled; do
 HTTP_DEADLINE=$((SECONDS+10))
 expected=4
 [[ $scenario != timeout ]] || expected=3
 [[ $scenario != cancelled ]] || expected=143
 http_get 'https://api.github.com/repos/NousResearch/hermes-agent/releases?per_page=100' application/json
 actual=$?
 [[ $actual == "$expected" ]] || { printf '%s: expected %s, got %s\n' "$scenario" "$expected" "$actual"; exit 1; }
done
''')

    def test_retention_preserves_receipt_with_impossible_source_date(self):
        self.shell(self.native_fixture_script()+r'''
create_backup 'IMAGE_PLACEHOLDER' || exit
file=$BACKUPS/hermes-backup-2026-09-12-000000-receipt.json
receipt=$(read_record "$file" | json '.captured="2020-01-01T00:00:00Z" | .source.tag="v2026.2.30"') || exit
atomic_record "$file" "$receipt" || exit
if validate_receipt "$receipt"; then exit 1; fi
retain_backups || exit
[[ -f "$file" && -f "$BACKUPS/hermes-backup-2026-09-12-000000.zip" ]]
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)), setup=self.zip_fixture)

    def test_workspace_menu_retries_invalid_input_and_allows_exit(self):
        self.shell(r'''
workspace_base() { printf '%s\n' "$ACCOUNT_HOME"; }
WORKSPACE_ARGUMENT=
select_workspace <<< $'0\n99\nnope\n\n999999999999999999999\n01' || exit
[[ $WORKSPACE_ARGUMENT == Workspace ]] || exit 1
WORKSPACE_ARGUMENT=
select_workspace <<< q; [[ $? == 130 ]] || exit 1
select_workspace < /dev/null; [[ $? == 130 ]]
''')

    def test_first_setup_menu_import_new_and_cancel(self):
        result = self.shell(r'''
choose_setup_action <<< 1 || exit
[[ $ACTION == restore ]] || exit 1
ACTION=setup
choose_setup_action <<< 2 || exit
[[ $ACTION == setup ]] || exit 1
choose_setup_action <<< q; [[ $? == 130 ]] || exit 1
choose_setup_action < /dev/null; [[ $? == 130 ]] || exit 1
choose_setup_action <<< $'3\n9\n2' || exit
[[ $ACTION == setup ]] || exit 1
choose_setup_action <<< Q; [[ $? == 130 ]]
''')
        self.assertIn('1) Restore a backup', result.stderr)
        self.assertNotIn('Import a backup', result.stderr)

    def test_first_setup_dispatches_import_with_existing_backups(self):
        self.shell(r'''
load_workspace && acquire_lock && prepare_directories && release_lock || exit
printf preserve > "$BACKUPS/existing.zip"
select_workspace() { return 0; }
operate_import() { dispatched=true; [[ $ACTION == restore ]]; }
operate <<< 1 || exit
[[ $dispatched == true && ! -e "$IMAGE_FILE" && $(cat "$BACKUPS/existing.zip") == preserve ]]
''')

    def test_launcher_config_rejects_missing_unknown_duplicate_and_invalid_values(self):
        changes = [
            ('MAX_JSON_BYTES=8388608\n', ''),
            ('MIN_JQ=1.7', 'MIN_JQ=1.7\nMIN_JQ=1.8'),
            ('MIN_JQ=1.7', 'UNRECOGNISED=1.7'),
            ('BACKUP_RETENTION_DAYS=14', 'BACKUP_RETENTION_DAYS=0'),
            ('CONTAINER_CPUS=1', 'CONTAINER_CPUS=01'),
            ('DASHBOARD_CONTAINER_PORT=9119', 'DASHBOARD_CONTAINER_PORT=65536'),
            ('DASHBOARD_PORTS=ezirius:19119 nala:29119 default:59119', 'DASHBOARD_PORTS=nala:29119'),
            ('DASHBOARD_PORTS=ezirius:19119 nala:29119 default:59119', 'DASHBOARD_PORTS=default:59119 default:29119'),
            ('HOME_PATH=Apps Data/Hermes/Home', 'HOME_PATH=../Home'),
            ('HOME_PATH=Apps Data/Hermes/Home', 'HOME_PATH=/tmp/Home'),
            ('WORKSPACE_BASE=/Volumes/Data', 'WORKSPACE_BASE=/Volumes/../Data'),
            ('TRUSTED_PATH=', 'TRUSTED_PATH=:'),
            ('TRUSTED_TOOL_USERS=ezirius', 'TRUSTED_TOOL_USERS=501'),
            ('TRUSTED_TOOL_USERS=ezirius', 'TRUSTED_TOOL_USERS=ezirius;root'),
            ('CONTAINER_MEMORY=2g', 'CONTAINER_MEMORY=$(touch /tmp/never-execute-config)'),
            ('MIN_SERVICE_RELEASE=v2026.9.14', 'MIN_SERVICE_RELEASE=v2026.2.30'),
            ('MIN_PODMAN_ARM64=6.1.1', 'MIN_PODMAN_ARM64=6.1.1-preview'),
            ('CONTAINER_STOP_TIMEOUT=45', 'CONTAINER_STOP_TIMEOUT=30'),
            ('DASHBOARD_CAPTURE_TIMEOUT=3', 'DASHBOARD_CAPTURE_TIMEOUT=2'),
        ]
        config = (ROOT/'config/hermes-container.conf').read_text()
        for old, new in changes:
            with self.subTest(setting=old):
                self.shell('load_launcher_config "$ACCOUNT_HOME/custom.conf"', expect=3,
                           setup=lambda root, value=config.replace(old, new):
                           (root/'custom.conf').write_text(value))

    def test_launcher_config_keeps_shell_text_literal(self):
        config = (ROOT/'config/hermes-container.conf').read_text().replace(
            'WORKSPACE_BASE=/Volumes/Data', 'WORKSPACE_BASE=/tmp/$(touch SHOULD_NOT_EXIST)')
        self.shell(r'''
cd "$SCRATCH" || exit
load_launcher_config "$ACCOUNT_HOME/custom.conf" || exit
[[ "$WORKSPACE_BASE" == '/tmp/$(touch SHOULD_NOT_EXIST)' && ! -e SHOULD_NOT_EXIST ]]
''', setup=lambda root: (root/'custom.conf').write_text(config))

    def test_custom_paths_ports_resources_and_retention_use_configuration(self):
        config = (ROOT/'config/hermes-container.conf').read_text()
        for old, new in [
            ('HOME_PATH=Apps Data/Hermes/Home', 'HOME_PATH=Custom Data/Private/Home'),
            ('BACKUPS_PATH=Apps Data/Hermes/Backups', 'BACKUPS_PATH=Custom Backups'),
            ('AGENT_DOCS_PATH=Apps Data/Hermes/Agent Docs', "AGENT_DOCS_PATH=O'Neil Work Files"),
            ('USER_DOCS_PATH=Documents/{workspace}/Apps Data/Hermes/User Docs', 'USER_DOCS_PATH=Personal/{workspace}/Files'),
            ('DASHBOARD_PORTS=ezirius:19119 nala:29119 default:59119', 'DASHBOARD_PORTS=workspace:30123 default:50123'),
            ('DASHBOARD_CONTAINER_PORT=9119', 'DASHBOARD_CONTAINER_PORT=9120'),
            ('CONTAINER_CPUS=1', 'CONTAINER_CPUS=2'),
            ('CONTAINER_MEMORY=2g', 'CONTAINER_MEMORY=3g'),
            ('CONTAINER_SHM_SIZE=512m', 'CONTAINER_SHM_SIZE=256m'),
            ('RESTART_POLICY=unless-stopped', 'RESTART_POLICY=on-failure'),
            ('BACKUP_RETENTION_DAYS=14', 'BACKUP_RETENTION_DAYS=21'),
        ]:
            config = config.replace(old, new)
        self.shell(r'''
load_launcher_config "$ACCOUNT_HOME/custom.conf" || exit
set_paths "$WORKSPACE" || exit
load_workspace && acquire_lock && prepare_directories || exit
[[ "$HERMES_DATA" == "$WORKSPACE/Custom Data/Private/Home" && -d "$HERMES_DATA" ]] || exit 1
[[ "$USER_DOCUMENTS" == "$ACCOUNT_HOME/Personal/Workspace/Files" && -d "$USER_DOCUMENTS" ]] || exit 1
[[ $(dashboard_port) == 30123 ]] || exit 1
[[ $(ACCOUNT_USER=someone dashboard_port) == 50123 ]] || exit 1
build_runtime 'IMAGE_PLACEHOLDER' service "$HERMES_DATA" || exit
[[ "${RUNTIME[*]}" == *'--cpus 2 --memory 3g --shm-size 256m'* &&
   "${RUNTIME[*]}" == *'127.0.0.1:30123:9120'* &&
   "${RUNTIME[*]}" == *'HERMES_DASHBOARD_PORT=9120'* &&
   "${RUNTIME[*]}" == *'--restart=on-failure'* ]] || exit 1
[[ $(config_bytes "$CONTAINER_MEMORY") == 3221225472 ]] || exit 1
cutoff=$(backup_cutoff); now=$(date -u +%s)
((now-cutoff>=21*86400 && now-cutoff<=21*86400+2)) || exit 1
release_lock
'''.replace('IMAGE_PLACEHOLDER', json.dumps(IMAGE)),
                   setup=lambda root: (root/'custom.conf').write_text(config))

    def test_configured_json_limits_are_enforced(self):
        self.shell(r'''
MAX_JSON_BYTES=10
printf '{"a":1}' | strict_json >/dev/null || exit
if printf '{"abcdefghij":1}' | strict_json >/dev/null; then exit 1; fi
MAX_JSON_BYTES=100; MAX_JSON_DEPTH=2
printf '[1]' | strict_json >/dev/null || exit
if printf '[[[1]]]' | strict_json >/dev/null; then exit 1; fi
''')

    def test_config_check_does_not_require_terminal_or_podman(self):
        result = subprocess.run([str(ROOT/'hermes-container.sh'), '--check-config'],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Configuration is valid:', result.stderr)

    def test_storage_checks_reject_overflow_and_malformed_byte_counts(self):
        self.shell(r'''
free_bytes() { printf '%s\n' "$test_available"; }
test_available=1073741824
for size in 18446744073709551616 9223372036854775807 9007199254740992 01 -1 1e9 '1+2' unknown ''; do
 require_disk_space "$BACKUPS" "$size" 2 1; [[ $? == 3 ]] || exit 1
done
for test_available in 18446744073709551616 01 -1 1e9 unknown ''; do
 require_disk_space "$BACKUPS" 1 1 1; [[ $? == 3 ]] || exit 1
done
# Exact boundary: two one-byte copies plus the configured reserve.
test_available=1073741826
require_disk_space "$BACKUPS" 1 2 1 || exit
test_available=1073741825
require_disk_space "$BACKUPS" 1 2 1; [[ $? == 3 ]] || exit 1
require_disk_space "$BACKUPS" 1 1 1 || exit
# Exercise the ZIP-summary path, not only the shared arithmetic helper.
run_capture() { CAPTURE_OUT=$SCRATCH/size; printf '1 file, 18446744073709551616 bytes uncompressed\n' > "$CAPTURE_OUT"; }
IMPORT_STAGE=$SCRATCH
check_import_capacity 2; [[ $? == 3 ]]
''')

    def test_restore_checks_space_before_copying_archive(self):
        result = self.shell(self.import_fixture_script()+r'''
free_bytes() { printf '1\n'; }
operate_import; [[ $? == 3 ]] || exit 1
[[ ! -e "$IMPORT_STAGE/input.zip" && $staged_calls == 0 && $live_calls == 0 && $backup_calls == 0 ]] || exit 1
[[ -f "$IMPORT_ARGUMENT" && $(cat "$HERMES_DATA/config.yaml") == 'old config' ]]
''', setup=self.zip_fixture)
        self.assertIn('insufficient free space', result.stderr)

    def test_restore_cancellation_before_archive_copy_preserves_home(self):
        self.shell(self.import_fixture_script()+r'''
require_disk_space() { INTERRUPTED=15; return 0; }
operate_import; [[ $? == 143 ]] || exit 1
[[ ! -e "$IMPORT_STAGE/input.zip" && $staged_calls == 0 && $live_calls == 0 ]] || exit 1
[[ -f "$IMPORT_ARGUMENT" && $(cat "$HERMES_DATA/config.yaml") == 'old config' ]]
''', setup=self.zip_fixture)

    def test_failed_restore_copy_preserves_diagnostics(self):
        result = self.shell(self.import_fixture_script()+r'''
run_capture() {
 [[ "$2" == /bin/cp ]] || return 99
 CAPTURE_ERR=$SCRATCH/copy-error
 printf 'simulated copy failure\n' > "$CAPTURE_ERR"
 return 3
}
operate_import; [[ $? == 3 ]] || exit 1
[[ $(cat "$IMPORT_STAGE/copy.stderr") == 'simulated copy failure' ]] || exit 1
[[ $staged_calls == 0 && $live_calls == 0 && $(cat "$HERMES_DATA/config.yaml") == 'old config' ]]
''', setup=self.zip_fixture)
        self.assertIn('could not copy restore archive; inspect', result.stderr)

    def test_source_parent_walk_rejects_unrelated_or_unnormalized_roots(self):
        self.shell(r'''
for root in / "$ACCOUNT_HOME/" "$ACCOUNT_HOME/other"; do
 add_source_parents "$root" "$ACCOUNT_HOME/Docs"; [[ $? == 4 ]] || exit 1
done
SOURCE_PARENTS=()
add_source_parents "$ACCOUNT_HOME" "$ACCOUNT_HOME/One/Two/Docs" || exit
[[ "${SOURCE_PARENTS[*]}" == "$ACCOUNT_HOME/One $ACCOUNT_HOME/One/Two" ]]
''')

    def test_config_rejects_binary_input(self):
        config = (ROOT/'config/hermes-container.conf').read_bytes()
        result = self.shell('load_launcher_config "$ACCOUNT_HOME/binary.conf"', expect=3,
                            setup=lambda root: (root/'binary.conf').write_bytes(config+b'\x00'))
        self.assertIn('NUL byte found', result.stderr)

    def test_config_errors_name_the_problem(self):
        config = (ROOT/'config/hermes-container.conf').read_text()
        cases = [
            (config+'MISSPELLED=1\n', 'unknown setting'),
            (config+'MIN_JQ=1.7\n', 'duplicate setting'),
            (config.replace('MIN_JQ=1.7', 'MIN_JQ=preview'), 'invalid value for MIN_JQ'),
            (config.replace('CONTAINER_STOP_TIMEOUT=45', 'CONTAINER_STOP_TIMEOUT=30'),
             'CONTAINER_STOP_TIMEOUT must exceed CONTAINER_STOP_GRACE'),
        ]
        for contents, message in cases:
            with self.subTest(message=message):
                result = self.shell('load_launcher_config "$ACCOUNT_HOME/custom.conf"', expect=3,
                                    setup=lambda root, value=contents: (root/'custom.conf').write_text(value))
                self.assertIn(message, result.stderr)

    def test_endpoint_and_process_settings_are_validated(self):
        self.shell(r'''
for value in http://api.example.test https://user:password@api.example.test https://api.example.test/path https://api.example.test:65536 https://-invalid.test https://api..test; do
 if config_value_valid RELEASE_API_ORIGIN "$value"; then exit 1; fi
done
config_value_valid RELEASE_API_ORIGIN https://api.example.test:8443 || exit
for value in 0 0.000 -1 1e2 0.1s '$(touch no)'; do
 if config_value_valid PROCESS_POLL_SECONDS "$value"; then exit 1; fi
done
config_value_valid PROCESS_POLL_SECONDS 0.025 || exit
if config_value_valid RELEASE_PAGE_SIZE 101; then exit 1; fi
config_value_valid RELEASE_PAGE_SIZE 2
''')

    def test_configured_endpoints_and_release_page_size_are_used(self):
        config = (ROOT/'config/hermes-container.conf').read_text()
        for old, new in [
            ('IMAGE_REGISTRY=docker.io', 'IMAGE_REGISTRY=images.example.test:5443'),
            ('RELEASE_API_ORIGIN=https://api.github.com', 'RELEASE_API_ORIGIN=https://releases.example.test'),
            ('REGISTRY_API_ORIGIN=https://registry-1.docker.io', 'REGISTRY_API_ORIGIN=https://registry.example.test'),
            ('REGISTRY_AUTH_ORIGIN=https://auth.docker.io', 'REGISTRY_AUTH_ORIGIN=https://auth.example.test'),
            ('REGISTRY_AUTH_SERVICE=registry.docker.io', 'REGISTRY_AUTH_SERVICE=registry.example.test'),
            ('RELEASE_PAGE_SIZE=100', 'RELEASE_PAGE_SIZE=2'),
        ]:
            config = config.replace(old, new)
        self.shell(r'''
load_launcher_config "$ACCOUNT_HOME/custom.conf" || exit
[[ "$IMAGE" == images.example.test:5443/nousresearch/hermes-agent ]] || exit 1
[[ "$REGISTRY_URL" == https://registry.example.test/v2/nousresearch/hermes-agent/manifests ]] || exit 1
[[ "$REGISTRY_AUTH_URL" == 'https://auth.example.test/token?service=registry.example.test&scope=repository:nousresearch/hermes-agent:pull' ]] || exit 1
http_get() {
 HTTP_TYPE=application/json
 case "$1" in
  'https://releases.example.test/repos/NousResearch/hermes-agent/releases?per_page=2')
   HTTP_BODY='[{"id":1,"tag_name":"v2026.9.1","published_at":"2026-09-01T00:00:00Z","draft":false,"prerelease":false}]'
   HTTP_LINK='<https://releases.example.test/repos/NousResearch/hermes-agent/releases?per_page=2&page=2>; rel="next"' ;;
  'https://releases.example.test/repos/NousResearch/hermes-agent/releases?per_page=2&page=2')
   HTTP_BODY='[{"id":2,"tag_name":"v2026.9.2","published_at":"2026-09-02T00:00:00Z","draft":false,"prerelease":false}]'
   HTTP_LINK= ;;
  *) return 99 ;;
 esac
}
latest=$(github_releases) || exit
[[ $(printf '%s' "$latest" | json -r .tag) == v2026.9.2 ]]
''', setup=lambda root: (root/'custom.conf').write_text(config))

    def test_json_and_command_output_limits_are_independent(self):
        self.shell(r'''
MAX_JSON_BYTES=4
MAX_STDOUT_BYTES=16
CAPTURE_OUT=$SCRATCH/output; CAPTURE_ERR=$SCRATCH/errors
printf '{"a":1}' > "$CAPTURE_OUT"; : > "$CAPTURE_ERR"
capture_within_limits || exit
if strict_json < "$CAPTURE_OUT"; then exit 1; fi
MAX_JSON_BYTES=16; MAX_STDOUT_BYTES=4
strict_json < "$CAPTURE_OUT" || exit
if capture_within_limits; then exit 1; fi
''')

if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LauncherTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped and not result.expectedFailures else 1)
