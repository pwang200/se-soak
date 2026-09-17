;; A1 unknown_imports — imports a function that is not a host function, so the
;; module is refused at EscrowCreate preflight before any escrow is created.
;;
;; Category:  unknown_imports   Lifecycle: preflight_reject
;; Expected EscrowCreate result: placeholder (temINVALID_BYTECODE / temMALFORMED)
;;
;; The import is from module "env" (not "host_lib") and names a function the
;; host does not provide; either fact is enough for the validator to reject.
;; Exercises audit finding 3.4: preflight allocates arena entries before
;; rejecting for the unresolved import.
(module
  (import "env" "does_not_exist" (func (result i32)))
  (func (export "escrow_finish") (result i32)
    (call 0)))
