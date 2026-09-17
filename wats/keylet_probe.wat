;; TEMPLATE keylet_probe — shared by the unknown_keylet / known_keylet
;; categories. escrow_finish reads a 20-byte account id from memory[0..20],
;; computes its AccountRoot keylet, and calls cache_le on it, then returns 1
;; unconditionally (a host error like LedgerObjNotFound reaches the guest as a
;; return value, not a trap). The 20-byte account id is a PATCH SLOT: the
;; driver rewrites it per cycle (see keylet_probe.patchspec.json), with a real
;; pool account (known_keylet -> loads a real object) or random bytes
;; (unknown_keylet -> LedgerObjNotFound). Same module, two access patterns, for
;; a direct WASM_TIMING comparison.
;;
;; The 20 sentinel bytes below (5EA7C0DE...FF) are located in the compiled
;; .wasm by wats/build_patchmaps.py and recorded as the account_id slot.
(module
  (import "host_lib" "accountroot_id"
    (func $accountroot_id (param i32 i32 i32 i32) (result i32)))
  (import "host_lib" "cache_le"
    (func $cache_le (param i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 0)
    "\5e\a7\c0\de\00\11\22\33\44\55\66\77\88\99\aa\bb\cc\dd\ee\ff")
  (func (export "escrow_finish") (result i32)
    ;; keylet(account bytes[0..20]) -> memory[32..64]
    (drop (call $accountroot_id
      (i32.const 0) (i32.const 20) (i32.const 32) (i32.const 32)))
    ;; cache the object (found -> slot >=0; missing -> -10). Ignore the result.
    (drop (call $cache_le (i32.const 32) (i32.const 32) (i32.const 0)))
    (i32.const 1)))
