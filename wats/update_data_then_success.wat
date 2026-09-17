;; C2 update_data_then_success — a stateful module. On the FIRST escrow_finish
;; the escrow has no Data field, so it writes one (set_data) and returns 0
;; (rejected; the write survives a reject and the escrow stays). On the SECOND
;; escrow_finish the Data field is present, so it returns 1 and the escrow is
;; removed. Requires the driver's multi-Finish mode (finish_removes, capped).
;;
;; Category:  update_data_then_success   Lifecycle: finish_removes (multi)  Gas: 5000
;; Expected: 1st Finish tecBYTECODE_REJECTED, terminal Finish tesSUCCESS.
;;
;; home_le_field(field, out_ptr, out_len) -> bytes written, or a negative host
;;   error code. `field` is the full SField code (type<<16)|nth, NOT the bare
;;   nth: sfData is VL(=7), nth 27 -> (7<<16)|27 = 458779. A negative return
;;   (FieldNotFound -2) means "no Data yet".
;; set_data(ptr, len) -> bytes stored. Writes memory[64..65] = 0x01.
;; Exercises set_data (the only ledger-mutating host fn) and home_le_field
;; (audit finding 3.11).
(module
  (import "host_lib" "home_le_field"
    (func $home_le_field (param i32 i32 i32) (result i32)))
  (import "host_lib" "set_data"
    (func $set_data (param i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 64) "\01")
  (func (export "escrow_finish") (result i32)
    (if (result i32)
      ;; read our own sfData (458779) into memory[0..32]; negative => absent
      (i32.lt_s
        (call $home_le_field (i32.const 458779) (i32.const 0) (i32.const 32))
        (i32.const 0))
      (then
        ;; no data yet: write one byte, reject (escrow stays for a retry)
        (drop (call $set_data (i32.const 64) (i32.const 1)))
        (i32.const 0))
      (else
        ;; data present: succeed, escrow removed
        (i32.const 1)))))
