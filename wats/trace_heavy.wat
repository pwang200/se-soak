;; C3 trace_heavy — calls the trace host function 5000 times, then returns 1.
;; Validates that release-mode trace is (near) zero-cost and does not leak per
;; call.
;;
;; Category:  trace_heavy   Lifecycle: finish_removes   Gas: 200000
;; Expected EscrowFinish result: tesSUCCESS.
;;
;; trace(msg_ptr, msg_len, data_type, data_ptr, data_len) -> no result.
;;   "soak trace message" is 18 bytes at offset 0; data_type 1 renders the
;;   8-byte region at offset 32. 5000 * 30 gas = 150000 for trace alone; the
;;   allowance (200000) leaves headroom. Adjust and report if it doesn't fit.
(module
  (import "host_lib" "trace"
    (func $trace (param i32 i32 i32 i32 i32)))
  (memory (export "memory") 1)
  (data (i32.const 0) "soak trace message")
  (data (i32.const 32) "\00\00\00\00\00\00\00\00")
  (func (export "escrow_finish") (result i32)
    (local $i i32)
    (loop $L
      (call $trace
        (i32.const 0)    ;; msg_ptr
        (i32.const 18)   ;; msg_len
        (i32.const 1)    ;; data_type
        (i32.const 32)   ;; data_ptr
        (i32.const 8))   ;; data_len
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br_if $L (i32.lt_s (local.get $i) (i32.const 5000))))
    (i32.const 1)))
