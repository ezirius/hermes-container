# Each CLI has its own schema. Convert both to one small inventory record.
def container_id:
    if $tool == "podman" then .Id else .configuration.id end;
def container_state:
    if $tool == "podman" then
        if $mode == "list" then .State else .State.Status end
    else
        # Apple snapshots used a string; ManagedContainer uses status.state.
        if (.status | type) == "string" then .status else .status.state end
    end;
def valid_id:
    type == "string" and test(
        if $tool == "podman" then "^[a-f0-9]{64}$"
        else "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" end);
def valid_state: type == "string" and test("^[a-z][a-z_-]*$");
def host_mount:
    if $tool == "podman" then
        {source: .Source, target: .Destination}
    else
        # tmpfs has no host path. Every other mount must expose its source.
        if .type == {tmpfs: {}} then empty
        else {source: .source, target: .destination} end
    end;

if type != "array" then error("inventory must be an array")
elif $mode == "list" then
    if all(.[]; (container_id | valid_id) and (container_state | valid_state))
       and ([.[] | container_id] | length == (unique | length)) then
        # A single string also gives jq a successful result for an empty list.
        [.[] | container_id] | join("\n")
    else error("invalid or duplicate inventory entry") end
else
    if length != 1 then error("inspect must return one container") else .[0] end
    | if container_id != $cid then error("inspect ID changed") else . end
    | if $tool == "podman" then
        if (.Config | type) != "object" or (.Config | has("Labels") | not)
        then error("missing Podman labels field") else . end
        | {id: $cid, state: container_state, mounts: .Mounts,
         labels: (if .Config.Labels == null then {} else .Config.Labels end)}
      else
        if has("id") and .id != $cid then error("Apple ID mismatch") else . end
        | {id: $cid, state: container_state, mounts: .configuration.mounts,
           labels: .configuration.labels}
      end
    | if (.state | valid_state) and (.mounts | type) == "array"
         and (.labels | type) == "object"
         and all(.labels[]; type == "string") then .
      else error("invalid inspect fields") end
    | .mounts |= map(host_mount)
    | if all(.mounts[]; (.source | type) == "string"
             and (.source | startswith("/"))
             and (.target | type) == "string"
             and (.target | startswith("/"))) then .
      else error("invalid host mount") end
end
