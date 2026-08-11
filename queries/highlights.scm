; Syntax highlighting for Skript
;
; The grammar stays generic (Clojure-style); head-symbol MEANING lives here
; (Scheme-style `(call . (identifier) @keyword (#match? ...))` patterns).
; Later patterns win for overlapping captures on the same node, so the
; ordering below matters: catch-alls first, specializations after.

; --- Comments ---
(line_comment) @comment.line
(shebang) @comment

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
  (#match? @keyword "^(def|defn|fn|if|let|case|begin|lambda|quote|set!|instance|method|and|or|not|else)$"))

; --- Literal keywords of the def/defn/fn rules ---
["def" "defn" "fn"] @keyword

; --- Builtin functions (head of a generic call) ---
(call
  .
  (identifier) @function.builtin
  (#match? @function.builtin "^(write|spawn|send|receive|load|display|map|map-get|map-set!|cons|fst|rst|list|car|cdr|abs|min|max|mod|rem|pow|avg|clamp|mag|arg|sqrt|nil\\?|fold|scan|keep|uniq|where|diff|flat|trans|chunk|part|take|drop|cat|rev|rot|outer|dot|zip|map2)$"))

; --- @-intrinsics (@rgba, @i64.add, @is-instance-of?) ---
((identifier) @function.builtin
 (#match? @function.builtin "^@"))

; --- Operators ---
((identifier) @operator
 (#match? @operator "^(\\+|-|\\*|/|=|>|<|>=|<=)$"))

; --- Bound names ---
(def name: (identifier) @function)
(defn name: (identifier) @function)

; --- Parameters, scoped to defn/fn arglists only ---
(defn params: (list (identifier) @variable.parameter))
(fn params: (list (identifier) @variable.parameter))

; --- Quoted forms ---
; ' FORM is data — highlight the whole quoted region as a constant.
(quote_lit _ @constant)

; --- Punctuation ---
["(" ")"] @punctuation.bracket
["[" "]"] @punctuation.bracket
["{" "}"] @punctuation.bracket
"'" @punctuation.special
(map_marker) @punctuation.special

; --- Errors ---
(ERROR) @error
