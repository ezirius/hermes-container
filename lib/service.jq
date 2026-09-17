# Match the actual container configuration before reusing or removing it.
# Require one value per environment key; conflicting duplicates are ambiguous.
def environment:
  type=="array" and all(.[]; type=="string" and contains("="))
  and (map(split("=")[0]) | length==(unique|length));
type=="array" and length==1 and (.[0] |
  .Id==$cid and (.Name | startswith($prefix) and endswith($suffix)
    and (ltrimstr($prefix) | rtrimstr($suffix) | test("^[0-9]{8}T[0-9]{6}Z$")))
  and (.ImageName==$image or .Config.Image==$image)
  and .Config.Labels["com.ezirius.hermesagent.managed"]=="true"
  and .Config.Labels["com.ezirius.hermesagent.action"]=="service"
  and .Config.Labels["com.ezirius.hermesagent.workspace_hash"]==$hash
  and .Config.Labels["com.ezirius.hermesagent.digest"]==$digest
  and .Config.Cmd==["gateway","run"] and .Config.WorkingDir=="/opt/data"
  # Reuse must keep tools in this container and in the selected Agent Docs.
  and (.Config.Env | environment and index("HERMES_DASHBOARD=1")!=null
    and index("HERMES_DASHBOARD_HOST=0.0.0.0")!=null
    and index("HERMES_DASHBOARD_PORT=9119")!=null
    and index("HERMES_UID="+$uid)!=null and index("HERMES_GID="+$gid)!=null
    and index("TERMINAL_ENV=local")!=null
    and index("TERMINAL_CWD="+$docs_target)!=null
    and index("HERMES_WRITE_SAFE_ROOT="+$docs_target)!=null)
  and .HostConfig.AutoRemove==false
  and .HostConfig.Privileged==false
  and .HostConfig.RestartPolicy.Name=="unless-stopped"
  and .HostConfig.PortBindings=={"9119/tcp":[{HostIp:"127.0.0.1",HostPort:$port}]}
  and ([.Mounts[] | {Source,Destination}] | sort_by(.Destination))==$mounts
  and all(.Mounts[]; .Type=="bind" and .RW==true)
  and (.State.Status|type)=="string"
  and (.ExecIDs==null or ((.ExecIDs|type)=="array"
    and all(.ExecIDs[]; type=="string" and length>0))))
