; Outline/structure for Skript
;   def  = (def NAME VALUE)
;   defn = (defn NAME (ARGS) BODY...)   - modern: NAME is the `name:` field
;   defn = (defn (NAME ARGS) BODY...)   - legacy: no `name:` field at all,
;                                         the name is the first arglist element
;   fn   = (fn (ARGS) BODY...)          - anonymous lambda, no name
;
; The legacy shape is not a historical curiosity: every defn in skript's own
; tree (290 of 290 across the 37 .pt files) uses it. Without the pattern below
; the outline matches none of them.

(def
  name: (identifier) @name
) @item

(defn
  name: (identifier) @name
  params: (list) @context
) @item

; `!name` excludes the modern shape, which the pattern above already handles;
; the leading `.` anchor takes only the FIRST arglist element, so the
; parameters after it are not mistaken for the name.
((defn
  !name
  params: (list
    .
    (identifier) @name)) @item
)

(fn
  params: (list) @context
) @item