;; A2 disabled_instructions — uses floating-point wasm instructions, which are
;; disabled for smart-escrow modules per XLS-0100, so the module is refused at
;; EscrowCreate preflight.
;;
;; Category:  disabled_instructions   Lifecycle: preflight_reject
;; Expected EscrowCreate result: placeholder (temINVALID_BYTECODE / temMALFORMED)
;;
;; Same preflight-reject path as A1 but for a different cause (forbidden
;; opcode rather than an unresolved import).
(module
  (func (export "escrow_finish") (result i32)
    (i32.trunc_f32_s
      (f32.add (f32.const 1.5) (f32.const 2.5)))))
