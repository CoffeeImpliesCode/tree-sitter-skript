; Bracket matching for Skript.
; Match ordinary delimiters and the map marker's closing brace.
("(" @open ")" @close)
("[" @open "]" @close)
("{" @open "}" @close)
(map_lit (map_marker) @open "}" @close)
