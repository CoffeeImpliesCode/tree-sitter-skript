; Syntax highlighting for Skript
;
; The grammar stays generic (Clojure-style); head-symbol MEANING lives here
; (Scheme-style `(call . (identifier) @keyword (#match? ...))` patterns).
; Later patterns win for overlapping captures on the same node, so the
; ordering below matters: catch-alls first, specializations after.

; --- Comments ---
(line_comment) @comment.line
(shebang) @comment
(block_comment) @comment.block
(datum_comment) @comment.block

; --- Atoms ---
(num_lit) @number
(kwd_lit) @constant
(string_lit) @string
(bool_lit) @constant.builtin
(nil_lit) @constant.builtin

; --- Catch-all identifier ---
; Non-operator, non-intrinsic identifiers are variables by default.
; Guarded so operator/intrinsic/@-prefixed forms never fall through here.
((identifier) @variable
 (#not-match? @variable "^[+\\-*/<>=?#@]"))

; --- Generic call head ---
; Head of any (...) list that is not a def/defn/fn or a builtin/special form.
(call
  .
  (identifier) @function)

; --- Special-form keywords (head of a generic call) ---
(call
  .
  (identifier) @keyword
  (#match? @keyword "^(def|defn|fn|let|letrec|letrec\\*|if|when|unless|cond|case|do|begin|quote|set!|eval|export|use|raise|try|guard|and|or|not|else)$"))

; --- Literal keywords of the def/defn/fn rules ---
["def" "defn" "fn"] @keyword

; --- Builtin functions (head of a generic call) ---
(call
  .
  (identifier) @function.builtin
  (#match? @function.builtin "^(write|spawn|send|receive|load|display|map|map-get|map-set!|cons|fst|rst|list|abs|min|max|mod|rem|pow|avg|clamp|mag|sqrt|nil\\?|fold|scan|keep|uniq|where|diff|flat|trans|chunk|part|take|drop|cat|rev|rot|outer|dot|zip|map2)$"))

; --- @-intrinsics (@rgba, @i64.add, @is-instance-of?) ---
((identifier) @function.builtin
 (#match? @function.builtin "^@"))

; --- Operators ---
((identifier) @operator
 (#match? @operator "^(\\+|-|\\*|/|=|!=|>|<|>=|<=)$"))

; --- Bound names ---
(def name: (identifier) @function)
(defn name: (identifier) @function)

; --- Parameters, scoped to defn/fn arglists only ---
(defn params: (list (identifier) @variable.parameter))
(fn params: (list (identifier) @variable.parameter))

; Legacy defn - the shape every defn in skript's own tree uses. There is no
; `name:` field, so the name is the FIRST arglist element and the patterns
; above capture it as a parameter. Placed after them so it wins for that node.
((defn !name params: (list . (identifier) @function)))

; --- Quoted forms ---
; ' FORM is data — highlight the whole quoted region as a constant.
(quote_lit _ @constant)

; --- Punctuation ---
["(" ")"] @punctuation.bracket
["[" "]"] @punctuation.bracket
["{" "}"] @punctuation.bracket
"'" @punctuation.special
(map_marker) @punctuation.special
"." @punctuation.delimiter

; --- Errors ---
(ERROR) @error
