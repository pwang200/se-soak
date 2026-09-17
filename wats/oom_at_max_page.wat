;; C4 oom_at_max_page — memory is capped at 1 page (min 1, max 1). escrow_finish
;; asks to grow by another page; memory.grow returns -1 (failure) per the wasm
;; spec rather than trapping. The module drops that -1 and returns 1.
;;
;; Category:  oom_at_max_page   Lifecycle: finish_removes   Gas: 1000
;; Expected EscrowFinish result: tesSUCCESS.
;;
;; Verifies xrpld propagates a failed memory.grow as -1 without trapping the vm.
(module
  (memory (export "memory") 1 1)
  (func (export "escrow_finish") (result i32)
    (drop (memory.grow (i32.const 1)))
    (i32.const 1)))
