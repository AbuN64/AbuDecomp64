Unmodified call/return lines from the port capture of `20261103T021115Z-monkey-1-3000`, `lockstep/port/calls.jsonl`, first call of each named function. Raw evidence intentionally retains the stack tail; comparison views must discard it.

The XPlus pair is the first XPlus_FillMemory call from `sp1p-ed2/pak-continue-legal/{base,all}/ls/calls.jsonl`; its declaration is `void *XPlus_FillMemory(void *, int, u32)` in `include/RESMGR/802A0888.H:7`. a[4] is 000081fd versus 0000a007; page/hash equality is recorded in the coordinator ed2 log.
