;; B4 trap_div_by_zero — escrow_finish() executes an integer divide by zero,
;; which traps the wasm engine.
;;
;; Category:  trap_div_by_zero   Lifecycle: cancel_removes   Gas: 1000
;; Expected EscrowFinish result: placeholder — likely tecFAILED_PROCESSING
;;   (a trap and an out-of-gas both abort the wasm abnormally). Report actual.
;;
;; The escrow survives the failed Finish and is removed by EscrowCancel after
;; CancelAfter.
(module
  (func (export "escrow_finish") (result i32)
    (i32.div_s (i32.const 1) (i32.const 0))))
