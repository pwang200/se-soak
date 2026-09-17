;; return_0 — escrow_finish() always returns 0, so xrpld rejects every EscrowFinish
;; (tecBYTECODE_REJECTED: fee charged, escrow stays). The escrow is removed by
;; EscrowCancel once CancelAfter has passed.
;;
;; Category:  return_0        Lifecycle: cancel_removes
;; Expected validated EscrowFinish result: tecBYTECODE_REJECTED
;;
;; No imports, no memory. Compiles to 46 bytes.
(module
  (func $escrow_finish (result i32)
    i32.const 0)
  (export "escrow_finish" (func $escrow_finish)))
