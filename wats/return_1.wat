;; return_1 — escrow_finish() always returns 1 (nonzero), so EscrowFinish succeeds
;; and removes the escrow.
;;
;; Category:  return_1        Lifecycle: finish_removes
;; Expected validated EscrowFinish result: tesSUCCESS
;;
;; No imports, no memory. Compiles to 46 bytes; the EscrowCreate fee scales
;; with code size (escrow_lib.create_fee_drops), so keep it minimal.
(module
  (func $escrow_finish (result i32)
    i32.const 1)
  (export "escrow_finish" (func $escrow_finish)))
