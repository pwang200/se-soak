;; C5 unknown_keylet — computes the AccountRoot keylet of a nonexistent account
;; and tries to cache it. cache_le returns LedgerObjNotFound (-10) to the guest,
;; which the guest ignores and returns 1, so the Finish still succeeds.
;;
;; Category:  unknown_keylet   Lifecycle: finish_removes   Gas: 6000
;; Expected EscrowFinish result: tesSUCCESS.
;;
;; accountroot_id(acct_ptr, acct_len, out_ptr, out_len) -> keylet bytes.
;; cache_le(obj_id_ptr, obj_id_len, cache_idx) -> slot, or -10 if not found.
;; The account id is 20 bytes of 0x11 (not the zero account, which the host may
;; treat specially). Confirms host errors reach the guest as return values
;; without trapping.
(module
  (import "host_lib" "accountroot_id"
    (func $accountroot_id (param i32 i32 i32 i32) (result i32)))
  (import "host_lib" "cache_le"
    (func $cache_le (param i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 0)
    "\11\11\11\11\11\11\11\11\11\11\11\11\11\11\11\11\11\11\11\11")
  (func (export "escrow_finish") (result i32)
    ;; keylet into memory[32..64]
    (drop (call $accountroot_id
      (i32.const 0)    ;; account_ptr
      (i32.const 20)   ;; account_len
      (i32.const 32)   ;; out_ptr
      (i32.const 32))) ;; out_len
    ;; try to cache the (nonexistent) object
    (drop (call $cache_le
      (i32.const 32)   ;; obj_id_ptr
      (i32.const 32)   ;; obj_id_len
      (i32.const 0)))  ;; cache_idx = 0 (host assigns)
    (i32.const 1)))
