;; B2 oog_execute — escrow_finish() loops forever, so the wasm engine runs out
;; of the Gas allowance during execution and aborts.
;;
;; Category:  oog_execute   Lifecycle: cancel_removes   Gas: 1000
;; Expected EscrowFinish result: placeholder (tecOUT_OF_GAS or
;;   tecFAILED_PROCESSING on this build; probe 5 saw tecFAILED_PROCESSING)
;;
;; The escrow survives the failed Finish and is removed by EscrowCancel after
;; CancelAfter, like return_0.
(module
  (func (export "escrow_finish") (result i32)
    (loop $L (br $L))
    (i32.const 1)))
