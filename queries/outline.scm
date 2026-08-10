; Outline/structure for Skript
;   def  = (def name value)
;   defn = (defn name (args) body...) — modern shape, name is an identifier
;   fn   = (fn (args) body)          — anonymous lambda, no name

(def
  name: (identifier) @name
) @item

(defn
  name: (identifier) @name
  params: (list) @context
) @item

(fn
  params: (list) @context
) @item
