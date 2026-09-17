# Parse raw JSON before fromjson can discard duplicate keys.
# The parser tracks keys per object, including escaped-equivalent spellings.
def peek: .tokens[.position] // null;
def next: .position += 1;
def expect($token):
  if peek == $token then next else error("invalid JSON punctuation") end;
def value($depth):
  def members($seen):
    peek as $key
    | if ($key | type) != "string" or ($key | startswith("\"") | not)
      then error("object key required") else . end
    | ($key | fromjson) as $name
    | if ($seen | has($name)) then error("duplicate JSON key") else . end
    | next | expect(":") | value($depth + 1)
    | if peek == "}" then next
      elif peek == "," then next | members($seen + {($name): true})
      else error("invalid JSON object") end;
  def elements:
    value($depth + 1)
    | if peek == "]" then next
      elif peek == "," then next | elements
      else error("invalid JSON array") end;
  if $depth > 64 then error("JSON nesting exceeds 64")
  elif peek == "{" then
    next | if peek == "}" then next else members({}) end
  elif peek == "[" then
    next | if peek == "]" then next else elements end
  elif peek == null then error("JSON value missing")
  elif (peek | test("^(true|false|null)$|^\"|^-?[0-9]")) then next
  else error("invalid JSON value") end;
. as $raw
| if utf8bytelength > 8388608 then error("JSON exceeds 8 MiB") else . end
| [scan("\"(?:[^\"\\\\\u0000-\u001f]|\\\\(?:[\"\\\\/bfnrt]|u[0-9a-fA-F]{4}))*\"|-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null|[{}\\[\\],:]|[ \\t\\r\\n]+")]
| if join("") != $raw then error("invalid JSON token") else . end
| {tokens: map(select(test("^[ \\t\\r\\n]+$") | not)), position: 0}
| value(0)
| if .position != (.tokens | length) then error("extra JSON root") else . end
| $raw | fromjson
| if any(.. | numbers; isinfinite or isnan or (floor == . and (fabs > 9007199254740991)))
  then error("JSON number outside safe range") else . end
