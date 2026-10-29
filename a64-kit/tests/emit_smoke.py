"""Smoke test for a64emit (suyu's AArch64 -> C translator), no game data needed.

    python a64-kit/tests/emit_smoke.py

Builds a64emit.dll on first use (lift/build_emit.py), then translates a few instruction forms TOTK uses and checks
that the translator handled them and that the generated runtime pieces are non-empty. This is the cheapest check
that the lifter's instruction pipeline is intact; the full instruction conformance test (tests/insn_test.py against
Unicorn) is not in this repository.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lift"))

import a64emit  # noqa: E402  (builds a64emit.dll through build_emit)


# (word, description): every form here is translated by suyu's front end, not by our control-flow code
WORDS = [
    (0xD503201F, "nop"),
    (0x91000400, "add x0, x0, #1"),
    (0xAA0103E0, "mov x0, x1"),
    (0xF9400020, "ldr x0, [x1]"),
    (0xF9000020, "str x0, [x1]"),
    (0x1E202800, "fadd s0, s0, s0"),
    (0x8B010000, "add x0, x0, x1"),
    (0xD5033FDF, "isb"),
]

RUNTIME_PARTS = {0: "suyu runtime header", 1: "suyu runtime C", 2: "FP estimate", 3: "FPX exact helpers"}


def main():
    items = [(0x08000000 + 4 * i, word) for i, (word, _) in enumerate(WORDS)]
    texts, flags = a64emit.translate(items)
    bad = []
    for (word, what), text, flag in zip(WORDS, texts, flags):
        unhandled = bool(flag & a64emit.UNHANDLED)
        if unhandled or not text.strip():
            bad.append("%08x %s: unhandled=%d text=%r" % (word, what, unhandled, text[:60]))
    for which, name in RUNTIME_PARTS.items():
        if not a64emit.runtime_text(which):
            bad.append("runtime_text(%d) (%s) is empty" % (which, name))
    if bad:
        print("a64emit smoke test FAILED")
        print("\n".join("  " + line for line in bad))
        return 1
    print("a64emit smoke test ok: %d instructions translated, %d runtime parts non-empty"
          % (len(texts), len(RUNTIME_PARTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
