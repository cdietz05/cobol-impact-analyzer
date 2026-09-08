import io
import unittest
from pathlib import Path

from cobol_impact_analyzer.copybook import CopybookResolver
from cobol_impact_analyzer.pco import discover_sources
from cobol_impact_analyzer.progress import NullProgress, Progress

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class ProgressTests(unittest.TestCase):
    def test_disabled_progress_writes_nothing(self):
        stream = io.StringIO()
        progress = Progress(stream=stream, enabled=False)
        progress.stage("hello")
        progress.step(1, 10, "x")
        progress.done("bye")
        self.assertEqual(stream.getvalue(), "")

    def test_null_progress_is_silent(self):
        # NullProgress owns stderr, so the only safe assertion is that it never
        # raises and never claims to be enabled.
        progress = NullProgress()
        self.assertFalse(progress.enabled)
        progress.stage("ignored")
        progress.step(1, 1)

    def test_stages_are_always_emitted(self):
        stream = io.StringIO()
        progress = Progress(stream=stream)
        progress.stage("first")
        progress.stage("second")
        self.assertIn("first", stream.getvalue())
        self.assertIn("second", stream.getvalue())

    def test_steps_are_throttled_but_the_last_one_always_lands(self):
        stream = io.StringIO()
        progress = Progress(stream=stream, min_interval=3600.0)
        for index in range(1, 6):
            progress.step(index, 5)
        lines = [line for line in stream.getvalue().splitlines() if line.strip()]
        # The first step and the final step; the middle ones are throttled away.
        self.assertEqual(len(lines), 2)
        self.assertIn("5/5", lines[-1])

    def test_a_broken_stream_disables_progress_instead_of_raising(self):
        stream = io.StringIO()
        progress = Progress(stream=stream)
        stream.close()
        progress.stage("this would raise on a closed stream")
        self.assertFalse(progress.enabled)

    def test_long_labels_are_shortened_from_the_left(self):
        stream = io.StringIO()
        progress = Progress(stream=stream)
        progress.step(1, 1, "/a/very/long/path/" + "x" * 200 + "/tail.pco")
        self.assertIn("tail.pco", stream.getvalue())
        for line in stream.getvalue().splitlines():
            self.assertLess(len(line), 120)


class ResolverLazinessTests(unittest.TestCase):
    def test_index_is_not_built_until_the_first_lookup(self):
        resolver = CopybookResolver([EXAMPLES / "copybooks"])
        self.assertIsNone(resolver._index)
        resolver.resolve("CUSTOMER")
        self.assertIsNotNone(resolver._index)

    def test_lookups_are_cached(self):
        resolver = CopybookResolver([EXAMPLES / "copybooks"])
        first = resolver.resolve("CUSTOMER")
        second = resolver.resolve("customer")
        self.assertEqual(first, second)
        self.assertIn("CUSTOMER", resolver._cache)

    def test_a_missing_copybook_resolves_to_none_and_is_remembered(self):
        resolver = CopybookResolver([EXAMPLES / "copybooks"])
        self.assertIsNone(resolver.resolve("NOSUCHBOOK"))
        self.assertIn("NOSUCHBOOK", resolver._cache)

    def test_unusual_extension_is_found_through_the_fallback_scan(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "WEIRD.xyzzy").write_text("       01  A PIC X(4).\n", encoding="utf-8")
            resolver = CopybookResolver([root])
            # .xyzzy is not in the default suffix set, so only the fallback finds it.
            self.assertIsNotNone(resolver.resolve("WEIRD"))
            self.assertIsNotNone(resolver._fallback_index)

    def test_pruned_directories_are_skipped(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "DECOY.cpy").write_text("       01  A PIC X.\n", encoding="utf-8")
            (root / "REAL.cpy").write_text("       01  B PIC X.\n", encoding="utf-8")
            resolver = CopybookResolver([root])
            self.assertIsNotNone(resolver.resolve("REAL"))
            self.assertIsNone(resolver.resolve("DECOY"))

    def test_custom_suffixes_replace_the_defaults(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "BOOK.cpb").write_text("       01  A PIC X.\n", encoding="utf-8")
            resolver = CopybookResolver([root], suffixes=[".cpb"])
            self.assertIsNotNone(resolver.resolve("BOOK"))

    def test_extensionless_copybooks_resolve_from_nested_directories(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = root / "copylib" / "prod" / "common"
            deep.mkdir(parents=True)
            # Mainframe copylibs ship members with no extension at all.
            (deep / "CU01TB01").write_text(
                "       01  CUST-REC.\n           05  CUST-NAME PIC X(30).\n",
                encoding="utf-8",
            )
            resolver = CopybookResolver([root / "copylib"])
            found = resolver.resolve("CU01TB01")
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "CU01TB01")
            # The default suffix set already covers them, so no full scan is needed.
            self.assertIsNone(resolver._fallback_index)


class DiscoveryTests(unittest.TestCase):
    def test_version_control_directories_are_not_scanned(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "hidden.pco").write_text("", encoding="utf-8")
            (root / "real.pco").write_text("", encoding="utf-8")
            found = discover_sources([root], ["*.pco"])
            self.assertEqual([path.name for path in found], ["real.pco"])

    def test_multiple_patterns_do_not_duplicate_a_file(self):
        found = discover_sources([EXAMPLES / "src"], ["*.pco", "*.pco", "*"])
        names = [path.name for path in found]
        self.assertEqual(len(names), len(set(names)))

    def test_a_missing_source_path_warns_rather_than_raising(self):
        stream = io.StringIO()
        progress = Progress(stream=stream)
        found = discover_sources([Path("/no/such/directory/anywhere")], ["*.pco"], progress)
        self.assertEqual(found, [])
        self.assertIn("does not exist", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
