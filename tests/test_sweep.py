import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from abu_decomp import DecompError, cli, sweep

_DIRS: list[tempfile.TemporaryDirectory] = []


def tearDownModule():
    for d in _DIRS:
        d.cleanup()


def rules(text: str) -> list[sweep.Rule]:
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "rules.toml"
        file.write_text(text)
        return sweep.load_rules([file])


class Tree:
    def __init__(self, files: dict[str, str]) -> None:
        self.dir = tempfile.TemporaryDirectory()
        _DIRS.append(self.dir)
        self.root = Path(self.dir.name)
        for name, text in files.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)

    def sweep(self, rule_text: str, **options):
        return sweep.run(rules(rule_text), {"t": self.root}, **options)


PATTERN = """
[[rule]]
id = "wide"
kind = "pattern"
why = "w"
paths = ["**/*.c"]
match = '\\*\\s*\\(\\s*u32\\s*\\*\\s*\\)'
unless = 'swap'
"""

DECL_USE = """
[[rule]]
id = "bytes"
kind = "decl-use"
why = "w"
paths = ["**/*.c", "**/*.h"]
declare = '^(?P<local>static\\s+)?(?:extern\\s+)?(?:u32|int)\\s+(?P<datum>\\w+)\\s*\\['
use = '\\(\\s*u8\\s*\\*\\s*\\)\\s*&?\\s*(?P<datum>\\w+)\\b(?!\\s*(?:->|\\.|\\[))'
"""

ARITY = """
[[rule]]
id = "args"
kind = "call-arity"
why = "w"
paths = ["**/*.c"]
"""


class PatternTests(unittest.TestCase):
    def test_hits_a_line_and_skips_unless_and_comments(self):
        tree = Tree(
            {
                "a.c": "int f(void) {\n  x = *(u32 *)p;\n  y = *(u32 *)q; /* swap */\n"
                "  /* *(u32 *)z */\n  w = *(u32 *)swap_it(p);\n}\n"
            }
        )
        result = tree.sweep(PATTERN)
        self.assertEqual([(h["file"], h["line"], h["in_fn"]) for h in result["hits"]], [("a.c", 2, "f")])
        self.assertEqual(result["rules"][0]["hits"], 1)

    def test_multiline_hit_is_the_line_the_match_starts_on(self):
        text = """
[[rule]]
id = "table"
kind = "pattern"
why = "w"
paths = ["**/*.c"]
multiline = true
match = 'u32\\s+\\w+\\[\\]\\s*=\\s*\\{[^}]*0x41424344'
"""
        tree = Tree({"d.c": "int a;\n\nu32 t[] = {\n  1,\n  0x41424344,\n};\n"})
        self.assertEqual([h["line"] for h in tree.sweep(text)["hits"]], [3])

    def test_a_named_datum_group_is_kept(self):
        text = PATTERN.replace("match = '", "match = '(?P<datum>D_\\w+).*").replace(
            "unless = 'swap'", "unless = 'nothing'"
        )
        tree = Tree({"a.c": "void f(void) { D_1 = *(u32 *)p; }\n"})
        self.assertEqual(tree.sweep(text)["hits"][0].get("datum"), "D_1")

    def test_globs_select_files_and_skip(self):
        tree = Tree({"a.c": "x = *(u32 *)p;\n", "tests/b.c": "x = *(u32 *)p;\n", "c.h": "x = *(u32 *)p;\n"})
        text = PATTERN.replace('paths = ["**/*.c"]', 'paths = ["**/*.c"]\nskip = ["tests/**"]')
        self.assertEqual([h["file"] for h in tree.sweep(text)["hits"]], ["a.c"])


class DeclUseTests(unittest.TestCase):
    def test_a_word_array_read_through_a_byte_cast_is_a_hit(self):
        tree = Tree(
            {
                "d.h": "extern u32 table[];\nextern int *ptr;\n",
                "a.c": "void f(void) {\n  b = (u8 *)table;\n  c = (u8 *)ptr;\n  d = (u8 *)table->x;\n"
                "  e = (u8 *)table[1];\n}\n",
            }
        )
        result = tree.sweep(DECL_USE)
        self.assertEqual([(h["line"], h["datum"]) for h in result["hits"]], [(2, "table")])
        self.assertEqual(result["hits"][0]["declared"], "d.h:1")

    def test_a_static_array_is_its_own_files_only(self):
        tree = Tree(
            {
                "a.c": "static u32 own[4];\nvoid f(void) { x = (u8 *)own; }\n",
                "b.c": "void g(u8 *own) { y = (u8 *)own; }\n",
            }
        )
        self.assertEqual([h["file"] for h in tree.sweep(DECL_USE)["hits"]], ["a.c"])


class ArityTests(unittest.TestCase):
    def hits(self, files: dict[str, str]):
        return Tree(files).sweep(ARITY)["hits"]

    def test_a_call_with_fewer_arguments_names_callee_and_caller(self):
        hits = self.hits(
            {
                "def.c": "float walk(Stream *p, float t) {\n  return t;\n}\n",
                "use.c": "extern void walk();\nvoid wrap(void) {\n  walk();\n  walk(a, b);\n  walk(f(1, 2));\n}\n",
            }
        )
        self.assertEqual(
            [(h["line"], h["fn"], h["callers"]) for h in hits], [(3, "walk", ["wrap"]), (5, "walk", ["wrap"])]
        )
        self.assertIn("takes 2 parameters", hits[0]["detail"])

    def test_declarations_members_and_variadics_are_not_calls(self):
        hits = self.hits(
            {
                "a.c": "int one(int a) {\n return a;\n}\nint many(int a, ...) {\n return a;\n}\n"
                "void g(void) {\n  s->one();\n  many();\n  int one(int a);\n  static int one();\n}\n"
            }
        )
        self.assertEqual(hits, [])

    def test_functions_defined_with_different_counts_are_skipped(self):
        hits = self.hits(
            {"a.c": "void f(int a) {\n}\nvoid g(void) {\n f();\n}\n", "b.c": "void f(int a, int b) {\n}\n"}
        )
        self.assertEqual(hits, [])

    def test_mode_any_also_flags_extra_arguments(self):
        text = ARITY + 'mode = "any"\n'
        hits = Tree({"a.c": "void f(int a) {\n}\nvoid g(void) {\n f(1, 2);\n}\n"}).sweep(text)["hits"]
        self.assertEqual(len(hits), 1)


class BaselineTests(unittest.TestCase):
    def test_a_baseline_marks_known_hits_and_survives_line_moves(self):
        tree = Tree({"a.c": "void f(void) {\n  x = *(u32 *)p;\n}\n"})
        base = tree.root / "base.tsv"
        first = tree.sweep(PATTERN)
        self.assertEqual(sweep.write_baseline(base, first["hits"]), 1)
        (tree.root / "a.c").write_text("/* moved */\n\nvoid f(void) {\n  x = *(u32 *)p;\n  y = *(u32 *)q;\n}\n")
        again = tree.sweep(PATTERN, baseline=base)
        self.assertEqual([(h["line"], h["new"]) for h in again["hits"]], [(4, False), (5, True)])
        self.assertEqual(again["rules"][0]["new"], 1)

    def test_a_row_that_names_no_hit_now_refuses(self):
        # RageWarsDecomp config/sweep-baseline.tsv at 388f01f3: 206 of its 219 files were gone after the SOURCE/
        # merges (this row's file among them) and no row matched any of the 184 hits, so it hid nothing.
        tree = Tree({"a.c": "x = *(u32 *)p;\n", "b.c": "y = 0;\n"})
        base = tree.root / "base.tsv"
        sweep.write_baseline(base, tree.sweep(PATTERN)["hits"])
        live = base.read_text()
        base.write_text(live + "wide\tsrc/AI/NAV/800C6D64.CPP\t2e407b5860be\t0\n")
        with self.assertRaises(DecompError) as gone:
            tree.sweep(PATTERN, baseline=base)
        self.assertEqual(gone.exception.key, "sweep.baseline")
        self.assertIn("1 rows name no hit now (1 name a file no tree has)", gone.exception.detail)
        self.assertIn("src/AI/NAV/800C6D64.CPP", gone.exception.detail)
        base.write_text(live + "wide\tb.c\t579349e6e4bf\t0\n")
        with self.assertRaises(DecompError) as fixed:
            tree.sweep(PATTERN, baseline=base)
        self.assertIn("1 rows name no hit now (0 name a file no tree has)", fixed.exception.detail)
        # a rewrite drops the stale row; a rule that did not run keeps its rows; a row of no loaded rule refuses
        self.assertEqual(tree.sweep(PATTERN, baseline=base, rewrite=True)["rules"][0]["new"], 0)
        both = PATTERN + PATTERN.replace('id = "wide"', 'id = "other"')
        base.write_text(live + "other\tgone.c\t579349e6e4bf\t0\n")
        self.assertEqual(tree.sweep(both, baseline=base, only=["wide"])["rules"][0]["new"], 0)
        base.write_text(live + "retired\ta.c\t579349e6e4bf\t0\n")
        with self.assertRaises(DecompError):
            tree.sweep(PATTERN, baseline=base)

    def test_identical_lines_are_numbered(self):
        hits = Tree({"a.c": "x = *(u32 *)p;\nx = *(u32 *)p;\n"}).sweep(PATTERN)["hits"]
        self.assertEqual(len(set(sweep.fingerprints(hits))), 2)


class RuleErrorTests(unittest.TestCase):
    def test_bad_rules_name_the_rule(self):
        for text, word in (
            (PATTERN.replace('kind = "pattern"', 'kind = "magic"'), "kind"),
            (PATTERN + "colour = 1\n", "unknown rule keys"),
            (DECL_USE.replace("(?P<datum>\\w+)\\s*\\['", "\\w+\\s*\\['"), "datum"),
            (PATTERN.replace("match = '", "match = '("), "rule wide"),
            (PATTERN + PATTERN, "twice"),
        ):
            with self.assertRaises(DecompError, msg=word) as raised:
                rules(text)
            self.assertIn(word, str(raised.exception))

    def test_a_rules_file_a_project_lacks_refuses_at_the_read_naming_its_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config" / "sweep.toml"  # the standard place; docs/sweep.toml is not tried
            with self.assertRaises(DecompError) as raised:
                sweep.load_rules([missing])
        self.assertEqual(raised.exception.key, "sweep.rules")
        self.assertIn(str(missing), str(raised.exception))

    def test_a_rule_names_only_trees_that_exist(self):
        with self.assertRaises(DecompError):
            Tree({}).sweep(PATTERN.replace('why = "w"', 'why = "w"\ntrees = ["ghost"]'))

    def test_only_unknown_rule(self):
        with self.assertRaises(DecompError):
            Tree({}).sweep(PATTERN, only=["nope"])


class FindingsAndCliTests(unittest.TestCase):
    def test_hits_become_findings_that_join_a_crash_bucket(self):
        tree = Tree({"def.c": "void f(int a) {\n}\n", "use.c": "void g(void) {\n f();\n}\n"})
        result = tree.sweep(ARITY)
        row = sweep.to_findings(result, {"t": tree.root})[0]
        self.assertEqual(
            (row["kind"], row["fn"], row["callers"], row["signature"]), ("sweep", "f", ["g"], "sweep args (t)")
        )
        self.assertTrue(row["evidence"].endswith("use.c:2"))

    def test_identical_lines_stay_distinct_findings(self):
        tree = Tree({"a.c": "x = *(u32 *)p;\nx = *(u32 *)p;\n"})
        rows = sweep.to_findings(tree.sweep(PATTERN), {"t": tree.root})
        self.assertEqual(len({r["fp"] for r in rows}), 2)

    def test_cli_exit_status_follows_new_block_hits_and_updates_the_baseline(self):
        tree = Tree(
            {"a.c": "x = *(u32 *)p;\n", "r.toml": PATTERN.replace('why = "w"', 'why = "w"\nseverity = "block"')}
        )
        base = tree.root / "b.tsv"
        args = ["sweep", "--rules", str(tree.root / "r.toml"), "--tree", f"t={tree.root}", "--baseline", str(base)]

        def call(extra):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(args + extra)
            return code, json.loads(out.getvalue()), err.getvalue()

        code, envelope, text = call([])
        self.assertEqual(code, 1)
        self.assertIn("wide [pattern, block]: 1 hits, 1 new", text)
        self.assertEqual(envelope["findings"][0]["key"], "sweep.wide")
        base.write_text("wide\tgone.c\t2e407b5860be\t0\n")
        code, envelope, _ = call([])
        self.assertEqual((code, envelope["findings"][0]["key"]), (1, "sweep.baseline"))
        code, envelope, _ = call(["--update-baseline"])
        self.assertEqual((code, envelope["result"]["baseline_written"]), (1, 1))  # judged before it was written
        self.assertNotIn("gone.c", base.read_text())
        code, envelope, _ = call([])
        self.assertEqual((code, envelope["result"]["rules"][0]["new"]), (0, 0))


SPELLING = """
[[rule]]
id = "spelling"
kind = "return-spelling"
why = "w"
paths = ["**/*.c", "**/*.h"]
"""
ALIAS = """
[[rule]]
id = "alias"
kind = "word-alias"
why = "w"
paths = ["**/*.c", "**/*.h"]
declare_paths = ["data/*.c"]
symbols = "syms.txt"
defines = ["VERSION_X"]
array = '^(?:unsigned int|u32)\\s+(?P<datum>\\w+)\\s*\\[(?P<count>\\d+)\\]\\s*='
narrow = '^extern\\s+(?:u8|u16|short)\\s+(?P<datum>\\w+)'
"""


class ReturnSpellingTests(unittest.TestCase):
    def hits(self, files):
        return Tree(files).sweep(SPELLING)["hits"]

    def test_a_call_with_the_result_pointer_of_a_by_value_function_is_a_hit(self):
        hits = self.hits(
            {
                "def.c": "CQuatern look(char *actor) {\n  CQuatern q;\n  return q;\n}\n",
                "use.c": "void user(void) {\n  CQuatern q;\n  q = look(a);\n  look(&q, a);\n}\n",
            }
        )
        self.assertEqual([(h["line"], h["fn"], h["callers"]) for h in hits], [(4, "look", ["user"])])
        self.assertIn("returns CQuatern by value", hits[0]["detail"])

    def test_a_void_declaration_of_a_by_value_function_is_a_hit(self):
        hits = self.hits(
            {
                "def.c": "CVector3 spot(float t) {\n  CVector3 v;\n  return v;\n}\n",
                "use.h": "extern void spot(CVector3 *, float);\n",
            }
        )
        self.assertEqual([(h["file"], h["line"]) for h in hits], [("use.h", 1)])

    def test_a_by_value_declaration_of_an_out_pointer_function_is_a_hit(self):
        hits = self.hits(
            {
                "def.c": "CQuatern *tilt(CQuatern *out, char *in) {\n  return out;\n}\n",
                "use.c": "extern CQuatern tilt(char *);\nextern CQuatern *tilt(CQuatern *, char *);\n",
            }
        )
        self.assertEqual([(h["file"], h["line"]) for h in hits], [("use.c", 1)])

    def test_scalars_pointers_and_consistent_spellings_are_not_hits(self):
        hits = self.hits(
            {
                "a.c": "int one(int a) {\n  return a;\n}\nCQuatern two(char *a) {\n  CQuatern q;\n  return q;\n}\n"
                "extern CQuatern two(char *);\nextern int one(int);\n"
                "void g(void) {\n  CQuatern q = two(a);\n  one(1);\n}\n"
            }
        )
        self.assertEqual(hits, [])

    def test_a_void_function_declared_with_a_value_is_a_hit_when_scalars_are_on(self):
        text = SPELLING + "scalars = true\n"
        files = {
            "def.c": "void dist(int a) {\n  other(a);\n}\nint kept(int a) {\n  return a;\n}\n",
            "use.h": "extern float dist(int);\nextern void kept(int);\nextern void dist(int);\n",
        }
        hits = Tree(files).sweep(text)["hits"]
        self.assertEqual([(h["file"], h["line"], h["fn"]) for h in hits], [("use.h", 1, "dist")])
        self.assertIn("defined void", hits[0]["detail"])
        self.assertEqual(Tree(files).sweep(SPELLING)["hits"], [])

    def test_a_float_function_declared_void_is_a_hit_when_scalars_are_on(self):
        text = SPELLING + "scalars = true\n"
        files = {
            "def.c": "float dist(int a) {\n  return sqrtf(a);\n}\n",
            "use.h": "extern void dist(int);\nextern float dist(int);\n",
        }
        hits = Tree(files).sweep(text)["hits"]
        self.assertEqual([(h["file"], h["line"], h["fn"]) for h in hits], [("use.h", 1, "dist")])
        self.assertIn("x87 stack", hits[0]["detail"])


class WordAliasTests(unittest.TestCase):
    FILES: ClassVar[dict[str, str]] = {
        "syms.txt": "table = 0x80100000;\nhalf = 0x80100006;\nlonely = 0x80200000;\nbyte = 0x80100000;\n",
        "data/80100000.c": "unsigned int table[4] = {\n 1, 2, 3, 4\n};\n",
        "use.h": "extern u16 half[];\nextern u16 lonely;\nextern u8 byte;\nextern int nothing;\n",
    }

    def test_a_narrow_extern_inside_a_word_array_is_a_hit(self):
        hits = Tree(self.FILES).sweep(ALIAS)["hits"]
        self.assertEqual([(h["datum"], h["line"]) for h in hits], [("half", 1), ("byte", 3)])
        self.assertIn("lies inside the word array table", hits[0]["detail"])

    def test_units_for_other_versions_do_not_count(self):
        files = dict(self.FILES)
        files["data/80100000.c"] = "#if defined(VERSION_Y)\nunsigned int table[4] = {\n 1, 2, 3, 4\n};\n#endif\n"
        self.assertEqual(Tree(files).sweep(ALIAS)["hits"], [])


if __name__ == "__main__":
    unittest.main()


# The RageWarsDecomp rule (config/sweep.toml): a suffix of 5 to 8 hex digits holding a decimal digit, a placeholder
# prefix, or a linker fill name.
ADDRESS_MATCH = (
    r"\b(?:[A-Za-z_]\w*?_(?=[0-9A-Fa-f]{0,7}[0-9])[0-9A-Fa-f]{5,8}"
    r"|(?:D|unk|func)_[0-9A-Fa-f]+|\w*LinkerAlignmentFill\w*)\b"
)
ADDRESS_NAME = f"""
[[rule]]
id = "address-name"
kind = "pattern"
severity = "block"
why = "an identifier that names an address"
paths = ["**/*.CPP", "**/*.H", "**/symbol_addrs.txt"]
match = '{ADDRESS_MATCH}'
"""


class AddressNameTests(unittest.TestCase):
    # Lines cut from RageWarsDecomp at cefb9e91 (DECOMP-STOCK census 11-02: 1,957 address-named identifiers).
    def test_address_named_identifiers_hit_and_a_new_one_is_new(self):
        tree = Tree(
            {
                "src/POOL.CPP": "unsigned int g_earlierPoolWords_8015ADB0[2] = { 0, 0x25BE20 };\n"
                "if (object->unk_0x086C == 4420) {\n"
                "unsigned int g_gameSettings[2] = { 0x800D36E8, 0 };\n",
                "src/FILL.H": "RestLinkerAlignmentFill12 g_GControllerbuttonremapBlock2LinkerAlignmentFill381 = {\n",
                "versions/us/symbol_addrs.txt": "D_95A8 = 0x000095A8; //\nPlayer_PlacePlayerForRound = 0x80012345;\n",
            }
        )
        base = tree.root / "base.tsv"
        first = tree.sweep(ADDRESS_NAME)
        self.assertEqual(
            sorted((h["file"], h["line"]) for h in first["hits"]),
            [("src/FILL.H", 1), ("src/POOL.CPP", 1), ("versions/us/symbol_addrs.txt", 1)],
        )
        sweep.write_baseline(base, first["hits"])
        (tree.root / "src/POOL.CPP").write_text(
            "unsigned int g_earlierPoolWords_8015ADB0[2] = { 0, 0x25BE20 };\nextern s32 func_80012345(void);\n"
        )
        again = tree.sweep(ADDRESS_NAME, baseline=base, rewrite=False)
        self.assertEqual([(h["file"], h["line"]) for h in again["hits"] if h["new"]], [("src/POOL.CPP", 2)])

    def test_a_rom_offset_suffix_of_five_to_seven_hex_digits_is_an_address_name(self):
        # Lines cut from RageWarsDecomp at cf3e77e7f: older-build ROM offsets (6 hex digits) and segment labels (5).
        tree = Tree(
            {
                "SOURCE/MENU.CPP": "extern char g_earlierMenuPaged_4064A8[];\n"
                "unsigned int words_EF968[1] = { 0 };\n"
                "if (state->flags & FLAG_ADDED) {\n"
                "s32 k_scoreTable_1234[4];\n"
                "s32 g_menuPageCount_FACED;\n",
            }
        )
        hits = tree.sweep(ADDRESS_NAME)["hits"]
        self.assertEqual([h["line"] for h in hits], [1, 2])  # 4 hex digits or no decimal digit is no address


WIDE_MEMBER = """
[[rule]]
id = "wide-member"
kind = "pattern"
why = "w"
paths = ["**/*.CPP", "**/*.H"]
match = '\\*\\s*\\(\\s*(?:u32|s32)\\s*\\*\\s*\\)\\s*&\\s*(?P<var>\\w+)\\s*(?:->|\\.)\\s*(?P<member>\\w+)'
unless_type = '(?:s32|u32|f32|int|float)'
"""

BARE_HOOK = """
[[rule]]
id = "bare-hook"
kind = "pattern"
why = "w"
paths = ["**/*.CPP", "**/*.H"]
match = '^\\s*(?P<var>fn|end)\\s*\\(\\s*\\)\\s*;'
unless_type = '.*\\(\\s*void\\s*\\)'
"""


class UnlessTypeTests(unittest.TestCase):
    # Shapes cut from RageWarsDecomp at 478a7685d: VISDATA2's float bounds read as bit patterns, CINEMA's onClose hook.
    HEADER = (
        "typedef float f32;\n"
        "typedef struct CVector3_t CVector3;\n"
        "struct CVector3_t { f32 x, y, z; };\n"
        "typedef struct { unsigned char kind; unsigned char flags; s32 word; } Packed;\n"
        "typedef void (*FuncPtr)(void);\n"
        "typedef void (*LooseHook)();\n"
    )

    def test_a_word_store_into_a_32_bit_member_is_no_hit_and_a_narrow_member_is(self):
        tree = Tree(
            {
                "include/T.H": self.HEADER,
                "SOURCE/V.CPP": "void f(Packed *record, CVector3 *position) {\n"
                "    CVector3 minimum, maximum;\n"
                "    *(s32 *)&minimum.x = 1;\n"
                "    *(s32 *)&maximum.z = 1;\n"
                "    *(u32 *)&record->kind = 2;\n"
                "    *(u32 *)&record->word = 2;\n"
                "    *(u32 *)&position->y = 3;\n"
                "    *(u32 *)&unknown->y = 4;\n"
                "}\n",
            }
        )
        hits = tree.sweep(WIDE_MEMBER)["hits"]
        self.assertEqual([h["line"] for h in hits], [5, 8])  # the u8 member, and a variable with no declaration

    def test_a_hook_prototyped_void_is_no_hit_and_an_unprototyped_one_is(self):
        tree = Tree(
            {
                "include/T.H": self.HEADER,
                "SOURCE/C.CPP": "extern FuncPtr g_onClose;\n"
                "void close(void) {\n"
                "    FuncPtr fn = g_onClose;\n"
                "    fn();\n"
                "}\n"
                "void later(LooseHook end) {\n"
                "    end();\n"
                "}\n",
            }
        )
        hits = tree.sweep(BARE_HOOK)["hits"]
        self.assertEqual([h["line"] for h in hits], [7])

    def test_unless_type_without_a_var_group_names_the_rule(self):
        with self.assertRaisesRegex(DecompError, "bare.*var"):
            rules(BARE_HOOK.replace("(?P<var>fn|end)", "(?:fn|end)").replace('"bare-hook"', '"bare"'))


RAW = """
[[rule]]
id = "raw"
kind = "raw-buffer"
why = "w"
paths = ["**/*.c", "**/*.h"]
fill = ['\\bread_rom\\s*\\(\\s*(?P<buf>[^,]+),']
carriers = ["pixels"]
unless = 'bswap'
defines = ["__BYTE_ORDER__=1234", "__ORDER_LITTLE_ENDIAN__=1234"]
"""


class RawBufferTests(unittest.TestCase):
    FILES: ClassVar[dict[str, str]] = {
        "types.h": (
            "typedef unsigned short u16;\ntypedef unsigned char u8;\nstruct Image { u8 *bytes; void *pixels; };\n"
        ),
        "load.c": (
            "void *g_colors;\n"
            "struct Image g_image;\n"
            "static void keep(void *colors) {\n"
            "    g_colors = colors;\n"
            "}\n"
            "int load(int n) {\n"
            "    u16 *palette;\n"
            "    u8 *bytes;\n"
            "    read_rom(palette, n);\n"
            "    read_rom(bytes, n);\n"
            "    keep(palette + 2);\n"
            "    if (palette[0] & 1) return 1;\n"
            "    return bytes[0] + __builtin_bswap16(palette[1]);\n"
            "}\n"
            "int use(int i) {\n"
            "    return ((u16 *)g_colors)[i] + ((u8 *)g_colors)[i] + ((int *)g_image.pixels)[i];\n"
            "}\n"
            "int other(u16 *table, int i) {\n"
            "    u16 *local;\n"
            "    local = table;\n"
            "    return local[i];\n"
            "}\n"
            "int ordered(int i) {\n"
            "#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__\n"
            "    return __builtin_bswap16(((u16 *)g_colors)[i]);\n"
            "#else\n"
            "    return ((u16 *)g_colors)[i];\n"
            "#endif\n"
            "}\n"
        ),
    }

    def hits(self):
        return Tree(self.FILES).sweep(RAW)["hits"]

    def test_a_filled_buffer_read_wide_is_a_hit_through_calls_globals_and_carriers(self):
        found = [(h["line"], h["datum"], h["origin"]) for h in self.hits()]
        self.assertEqual(
            found,
            [(12, "palette", "load.c:9"), (16, "g_colors", "load.c:9"), (16, "pixels", "carrier pixels")],
        )

    def test_byte_access_unless_lines_and_the_other_byte_order_are_not_hits(self):
        lines = {h["line"] for h in self.hits()}
        self.assertNotIn(13, lines)  # bytes[0] is a byte, palette[1] is swapped
        self.assertFalse(lines & {21, 25, 27})  # never filled; the swapped branch; the big-endian branch

    def test_a_fill_without_a_buf_group_names_the_rule(self):
        with self.assertRaisesRegex(DecompError, "raw.*buf"):
            rules(RAW.replace("(?P<buf>[^,]+)", "([^,]+)"))


BYTE_ALIAS = """
[[rule]]
id = "bytes"
kind = "byte-alias"
why = "w"
paths = ["**/*.c", "**/*.h"]
copies = '\\bmemcpy\\s*\\((?P<datum>[^;]*?),\\s*(?P<bytes>0x[0-9A-Fa-f]+|\\d+)\\s*\\)'
"""


class ByteAliasTests(unittest.TestCase):
    """Wave-5 byte-alias class: the actionReady slot byte, the navigation flags halfword view, partial-word copies."""

    def hits(self, files):
        return Tree(files).sweep(BYTE_ALIAS)["hits"]

    HEADER = (
        "typedef struct Actor { u16 model; u8 pad[2]; s32 actionReady; f32 speed; char name[8]; } Actor;\n"
        "typedef struct Pair { Actor actor; u16 halves[2]; } Pair;\n"
    )

    def test_a_byte_view_of_a_wide_member_is_a_hit_and_an_address_is_not(self):
        hits = self.hits(
            {
                "a.h": self.HEADER,
                "use.c": "void f(Actor *actor, Pair p) {\n"
                "  ((u8 *)&actor->actionReady)[2] = 1;\n"
                "  *(u16 *)&p.actor.speed = 2;\n"
                "  g((u8 *)&actor->actionReady + 2);\n"
                "  ((u8 *)&actor->model)[1] = 3;\n"
                "  ((u8 *)&actor->name)[1] = 4;\n"
                "}\n",
            }
        )
        self.assertEqual(
            [(h["line"], h["access"], h["declared"]) for h in hits],
            [(2, "u8 view", "s32"), (3, "u16 view", "f32"), (5, "u8 view", "u16")],
        )

    def test_an_offset_view_is_judged_against_the_o32_layout(self):
        hits = self.hits(
            {
                "a.h": self.HEADER,
                "use.c": "void f(Actor *a, Pair *p) {\n"
                "  *(u16 *)((u8 *)a + 6) = 1;\n"  # inside actionReady (s32 at 4)
                "  *(s32 *)((u8 *)a + 4) = 1;\n"  # actionReady itself
                "  *(u32 *)((char *)&p->actor + 0x10) = 1;\n"  # four bytes of name: chars, no hit
                "  *(u32 *)((u8 *)p + 0x14) = 1;\n"  # the two halves read as one word
                "  *(u8 *)((u8 *)a + 1) = 1;\n"  # a byte of model
                "}\n",
            }
        )
        self.assertEqual([(h["line"], h["offset"]) for h in hits], [(2, 6), (5, 0x14), (6, 1)])
        self.assertIn("s32@0x4", hits[0]["declared"])

    def test_a_union_of_a_narrow_and_a_wide_scalar_is_a_hit_and_a_view_union_is_not(self):
        hits = self.hits(
            {
                "a.h": "union Flags { u32 v0; u16 v1; };\n"
                "union Views { struct { s16 type; void *state; } msg; Actor actor; u8 bytes[8]; };\n"
                "union Same { f32 f; u32 w; };\n",
            }
        )
        self.assertEqual([(h["file"], h["line"]) for h in hits], [("a.h", 1)])

    def test_a_copy_of_a_part_of_a_word_is_a_hit(self):
        hits = self.hits({"use.c": "void f(void) {\n  memcpy(dst, src, 3);\n  memcpy(dst, src, 8);\n}\n"})
        self.assertEqual([(h["line"], h["access"]) for h in hits], [(2, "copy of 3 bytes")])
