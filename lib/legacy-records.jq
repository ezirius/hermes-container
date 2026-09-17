# Read-only validation for preserved records from the previous launcher.
# Record schemas. Keep all lifecycle decisions in Bash, not in these predicates.
def keys_are($names): type == "object" and (keys == ($names | sort));
def integer: type == "number" and floor == . and . >= 0 and . <= 9007199254740991;
def token: type == "string" and test("^[a-f0-9]{32}$");
def hash: type == "string" and test("^[a-f0-9]{64}$");
def digest: type == "string" and test("^sha256:[a-f0-9]{64}$");
def utc: type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
  and (. as $original | try ((fromdateiso8601 | todateiso8601) == $original) catch false);
def image:
  keys_are(["tag","id","published","arm64","amd64","platform","digest"])
  and (.tag | type == "string" and test("^v?[0-9]{4}\\.[0-9]{1,2}\\.[0-9]{1,2}(\\.[0-9]+)?$"))
  and (.id | integer and . > 0) and (.published | utc)
  and (.arm64 | digest) and (.amd64 | digest)
  and (.platform == "amd64" or .platform == "arm64") and .digest == .[.platform];
def backup:
  keys_are(["name","hash","receipt_hash","attempt"])
  and (.name | type == "string" and test("^hermes-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}\\.zip$"))
  and (.hash | hash) and (.receipt_hash | hash) and (.attempt | integer and . > 0);
def pending:
  keys_are(["id","action","before","target","phase","attempt","backup","sequence"])
  and (.id | token) and (.action == "setup" or .action == "chat")
  and (.before == null or (.before | image)) and (.target | image)
  and (.phase == "backup-pending" or .phase == "backup-verified" or .phase == "run-intent" or .phase == "run-succeeded")
  and (.attempt | integer and . > 0) and (.sequence | integer and . > 0)
  and (.backup == null or (.backup | backup))
  and (if .before == null then .backup == null and .action == "setup" and .phase != "backup-pending"
       elif .phase == "backup-pending" then .backup == null
       else .backup != null and .backup.attempt == .attempt end);
def state:
  keys_are(["schema","owner","workspace","generation","sequence","accepted","pending"])
  and .schema == 1 and .owner == "bash" and (.workspace | type == "string" and startswith("/"))
  and (.generation | integer and . > 0) and (.sequence | integer)
  and (.accepted == null or (.accepted | image))
  and (.pending == null or ((.pending | pending) and .pending.before == .accepted and .pending.sequence == .sequence + 1));
def receipt:
  keys_are(["schema","workspace","operation","sequence","name","hash","captured","source","validated"])
  and .schema == 1 and (.workspace | type == "string" and startswith("/"))
  and (.operation | token) and (.sequence | integer and . > 0)
  and (.name | type == "string" and test("^hermes-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}\\.zip$"))
  and (.hash | hash) and (.captured | utc) and (.source | image) and .validated == true;
if $kind == "state" then state
elif $kind == "image" then image
elif $kind == "receipt" then receipt
elif $kind == "settings" then keys_are(["schema","check_updates_on_chat"]) and .schema == 1 and (.check_updates_on_chat | type == "boolean")
else false end
