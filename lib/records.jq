# The selected image is configuration; it does not record successful setup.
def keys_are($names): type == "object" and keys == ($names | sort);
def digest: type == "string" and test("^sha256:[a-f0-9]{64}$");
def utc: type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
  and (. as $original | try ((fromdateiso8601 | todateiso8601) == $original) catch false);
def image:
  keys_are(["tag","platform","digest"])
  and (.tag | type == "string" and test("^v?[0-9]{4}\\.[0-9]{1,2}\\.[0-9]{1,2}(\\.[0-9]+)?$"))
  and (.platform == "amd64" or .platform == "arm64") and (.digest | digest);
def receipt:
  keys_are(["schema","workspace","operation","name","hash","captured","source"])
  and .schema == 2 and (.workspace | type == "string" and startswith("/"))
  and (.operation | type == "string" and test("^[a-f0-9]{32}$"))
  and (.name | type == "string" and test("^hermes-backup-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}\\.zip$"))
  and (.hash | type == "string" and test("^[a-f0-9]{64}$"))
  and (.captured | utc) and (.source | image);
if $kind == "image" then image
elif $kind == "receipt" then receipt
else false end
