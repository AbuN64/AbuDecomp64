"""Real cdecl capture: only declared argument words reach comparison or reporting."""

import unittest
from dataclasses import replace
from pathlib import Path

from abu_decomp import arity
from abu_decomp.behave import percall, prototype, replay

FIXTURE = Path(__file__).parent / "fixtures/trace_arity/calls.jsonl"
DECLARATIONS = {
    "Cache_AllocatePinnedResourceBlock": "s32 Cache_AllocatePinnedResourceBlock(s32 a, s32 b, s32 c);",
    "SetN64_StartSessionThreads": "void SetN64_StartSessionThreads(void);",
}


def protos(name):
    return prototype.parse_declaration(DECLARATIONS[name], name, source="recorded-source")


class TraceArityTests(unittest.TestCase):
    def test_real_slice_has_only_three_and_zero_words_in_comparison_views(self):
        calls, _ = percall.read_calls(FIXTURE, "port")
        for call in calls.values():
            proto = protos(call.fn)
            clean = percall.bounded_call(proto, call, "port")
            count = len(proto.params)
            self.assertEqual(len(call.words), 8)  # preserve the regression evidence
            self.assertEqual(len(clean.words), count)
            self.assertTrue(all(int(k) < count for k in clean.writes))
            peer = replace(call, words=call.words[:count] + [0] * (8 - count), writes={})
            self.assertEqual(percall.diff_args(proto, peer, call), [])
            self.assertEqual(percall.diff_windows(proto, peer, call), [])
            self.assertEqual(
                percall.align({(call.fn, call.caller, 0): peer}, {(call.fn, call.caller, 0): call}, protos)[
                    (call.fn)
                ].verdict,
                "behaves",
            )

    def test_meaningful_argument_still_diverges_and_report_has_no_stack_tail(self):
        calls, _ = percall.read_calls(FIXTURE, "port")
        key = next(k for k in calls if k[0] == "Cache_AllocatePinnedResourceBlock")
        call = calls[key]
        peer = replace(call, words=[1, *call.words[1:]])
        verdict = percall.align({key: peer}, {key: call}, protos)[call.fn]
        self.assertEqual(verdict.verdict, "differs-args")
        self.assertEqual(len(verdict.first["port"]["args"]), 3)
        self.assertEqual(len(verdict.first["oracle"]["args"]), 3)

    def test_unknown_variadic_and_aggregate_arity_refuse(self):
        for proto in (
            prototype.inferred("missing"),
            prototype.parse_declaration("int variadic(int n, ...);", "variadic"),
            prototype.parse_declaration("int aggregate(struct Obj n);", "aggregate"),
        ):
            call = percall.Call(proto.name, "root", 0, 0, 0, list(range(8)), writes={"0": "x"})
            key = (proto.name, "root", 0)
            for side in ("port", "oracle"):
                with self.subTest(name=proto.name, side=side), self.assertRaisesRegex(arity.ArityError, proto.name):
                    percall.bounded_call(proto, call, side)
            for oracle, port in (({key: call}, {key: call}), ({key: call}, {}), ({}, {key: call})):
                with (
                    self.subTest(name=proto.name, oracle=bool(oracle), port=bool(port)),
                    self.assertRaisesRegex(arity.ArityError, proto.name),
                ):
                    percall.align(oracle, port, lambda name, proto=proto: proto)
            with self.assertRaisesRegex(arity.ArityError, proto.name):
                replay.native_words(proto, None, {})

    def test_o32_alignment_and_cdecl_have_different_word_counts(self):
        proto = prototype.parse_declaration("void f(int n, double x);", "f")
        self.assertEqual(len(arity.trace_fields(proto, "port", list(range(8)), {})[0]), 3)
        self.assertEqual(len(arity.trace_fields(proto, "oracle", list(range(8)), {})[0]), 4)

    def test_real_void_and_three_word_replay_ignore_unused_registers(self):
        for name in DECLARATIONS:
            sample = replay.Sample(
                movie="real",
                pak="on",
                function=name,
                index=0,
                frame=0,
                sp=0,
                args=[1, 2, 3, 0xDEADBEEF],
                f12=0,
                f14=0,
                stack=[],
                reads=[],
                stores=[],
                ret=[0, 0],
                f0=0,
            )
            self.assertEqual(replay.native_words(protos(name), sample, {})[0], [1, 2, 3][: len(protos(name).params)])


class XPlusRegressionTests(unittest.TestCase):
    def test_real_ed2_stack_word_four_is_not_an_argument_difference(self):
        proto = prototype.parse_declaration(
            "void *XPlus_FillMemory(void *p, int v, u32 n);", "XPlus_FillMemory", source="RESMGR/802A0888.H:7"
        )
        calls = [
            next(iter(percall.read_calls(FIXTURE.with_name(f"xplus-{side}.jsonl"), "port")[0].values()))
            for side in ("base", "all")
        ]
        self.assertEqual([c.words[4] for c in calls], [0x81FD, 0xA007])
        self.assertEqual([len(percall.bounded_call(proto, c, "port").words) for c in calls], [3, 3])
        self.assertEqual(percall.diff_args(proto, *calls), [])
