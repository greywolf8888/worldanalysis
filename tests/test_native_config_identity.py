"""synthetic_temp direct native identity tests; never open a production Store.

All SQLite, sources, implementation modules and publications are in unique
workspace-local temporary directories. No real course extraction is claimed.
"""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock
import uuid

from test_extraction_cache import raw_extract
from worldanalysis.contracts import ContractError
from worldanalysis.store import Store, _config_value

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "tests" / "document_fixtures" / ".runs"
_DEFAULT_CALLS = []
MODULE_SOURCE = '''from test_extraction_cache import raw_extract
PROVIDER_ID = "synthetic-cache-native"
PROVIDER_VERSION = "1"
calls = []
def helper():
    return "synthetic_temp helper A"
def extract(path, outdir, *, options=None):
    calls.append(1)
    raw = raw_extract(path, outdir)
    raw["blocks"][0]["text"] = helper()
    raw["metadata"]["evidence_kind"] = "synthetic_temp"
    return raw
'''


def _provider(function):
    function.provider_id = "synthetic-cache-native"
    function.provider_version = "1"
    return function


class NativeConfigIdentityTests(unittest.TestCase):
    def setUp(self):
        RUNS.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-temp-native-config-", dir=RUNS)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assertTrue(self.root.resolve().is_relative_to(ROOT))
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.project = self.store.create_project(topic="synthetic_temp native default identity")
        self.source = self.root / "source.txt"
        self.source.write_text("synthetic_temp unchanged source", encoding="utf-8")

    def _counts(self):
        return tuple(self.store.conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                     for table in ("sources", "assets", "asset_revisions", "jobs", "publications", "events"))

    def _module(self):
        path = self.root / "synthetic_temp_implementation.py"
        path.write_text(MODULE_SOURCE, encoding="utf-8")
        name = "synthetic_temp_native_" + uuid.uuid4().hex
        module = types.ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        self.addCleanup(lambda: sys.modules.pop(name, None))
        exec(compile(MODULE_SOURCE, str(path), "exec"), module.__dict__)
        return module, path

    def test_same_provider_version_changed_implementation_creates_new_job(self):
        calls = []

        @_provider
        def implementation_a(path, outdir, *, options=None):
            calls.append("A")
            raw = raw_extract(path, outdir)
            raw["blocks"][0]["text"] = "synthetic_temp implementation A"
            return raw

        @_provider
        def implementation_b(path, outdir, *, options=None):
            calls.append("B")
            raw = raw_extract(path, outdir)
            raw["blocks"][0]["text"] = "synthetic_temp implementation B"
            return raw

        options = {"synthetic_temp": True}
        first = self.store.ingest(self.project, [self.source], implementation_a, options=options)
        second = self.store.ingest(self.project, [self.source], implementation_b, options=options)
        self.assertEqual(calls, ["A", "B"])
        self.assertNotEqual(first["publish_id"], second["publish_id"])
        self.assertEqual(first["assets"], second["assets"])
        self.assertEqual(second["blocks"][0]["text"], "synthetic_temp implementation B")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        configs = {json.loads(row[0])["config_version"] for row in self.store.conn.execute("SELECT provider_json FROM jobs")}
        self.assertEqual(len(configs), 2)
        self.assertTrue(all(config.startswith("config-") and len(config) == 39 for config in configs))

    def test_exact_replay_same_callable_options_runs_once(self):
        calls = []

        @_provider
        def counted(path, outdir, *, options=None):
            calls.append(path.name)
            return raw_extract(path, outdir)

        second_source = self.root / "second.txt"
        second_source.write_text("synthetic_temp second source", encoding="utf-8")
        options = {"synthetic_temp": True, "mode": "stable"}
        before = _config_value(counted)
        module_path = Path(__file__).absolute()
        module_reads = []
        original_open = Path.open

        def recording_open(path, *args, **kwargs):
            if Path(path).absolute() == module_path:
                module_reads.append(1)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", recording_open):
            first = self.store.ingest(self.project, [self.source, second_source], counted, options=options)
            self.assertEqual(len(calls), 2)
            second = self.store.ingest(self.project, [self.source, second_source], counted, options=options)
        self.assertEqual(len(calls), 2, "one extraction per source; replay must not call extractor")
        self.assertEqual(module_reads, [1, 1], "one module read per batch, not per input")
        self.assertEqual(before, _config_value(counted), "counter mutation must not manufacture a new identity")
        self.assertEqual(first["bundle_id"], second["bundle_id"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        self.assertEqual(len(self.store.list_bundles(self.project)), 1)

    def test_same_wrapper_helper_source_change_invalidates_default(self):
        module, path = self._module()
        wrapper = module.extract
        identity = _config_value(wrapper)
        first = self.store.ingest(self.project, [self.source], wrapper)
        changed_source = MODULE_SOURCE.replace("synthetic_temp helper A", "synthetic_temp helper B")
        path.write_text(changed_source, encoding="utf-8")
        reloaded = {"__name__": module.__name__, "__file__": str(path)}
        exec(compile(changed_source, str(path), "exec"), reloaded)
        module.helper = reloaded["helper"]
        self.assertIs(module.extract, wrapper)
        self.assertEqual(identity, _config_value(wrapper))
        second = self.store.ingest(self.project, [self.source], wrapper)
        self.assertEqual(module.calls, [1, 1])
        self.assertNotEqual(first["bundle_id"], second["bundle_id"])
        self.assertEqual(first["blocks"][0]["text"], "synthetic_temp helper A")
        self.assertEqual(second["blocks"][0]["text"], "synthetic_temp helper B")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_explicit_config_preserves_published_replay_and_readability(self):
        module, path = self._module()
        cfg1, cfg2 = "synthetic-temp-published-v1", "synthetic-temp-published-v2"
        first = self.store.ingest(self.project, [self.source], module.extract, config_version=cfg1)
        module.helper = lambda: "synthetic_temp explicit helper B"
        path.unlink()  # Explicit configuration must not require/read implementation source.
        replay = self.store.ingest(self.project, [self.source], module.extract, config_version=cfg1)
        self.assertEqual(first["bundle_id"], replay["bundle_id"])
        self.assertEqual(module.calls, [1])
        fresh_source = self.root / "unregistered.txt"
        fresh_source.write_text("synthetic_temp not registered on identity failure", encoding="utf-8")
        before = self._counts()
        with self.assertRaisesRegex(ContractError, "explicit config_version"):
            self.store.ingest(self.project, [fresh_source], module.extract)
        self.assertEqual(self._counts(), before)

        class OpaqueNative:
            provider_id = "synthetic-cache-native"
            provider_version = "1"

            def __call__(self, path, outdir, *, options=None):
                return module.extract(path, outdir, options=options)

        opaque = OpaqueNative()
        with self.assertRaisesRegex(ContractError, "explicit config_version"):
            self.store.ingest(self.project, [fresh_source], opaque)
        self.assertEqual(self._counts(), before)
        opaque_replay = self.store.ingest(self.project, [self.source], opaque, config_version=cfg1)
        self.assertEqual(opaque_replay["bundle_id"], first["bundle_id"])
        self.assertEqual(module.calls, [1])
        second = self.store.ingest(self.project, [self.source], module.extract, config_version=cfg2)
        self.assertEqual(module.calls, [1, 1])
        self.assertEqual(second["blocks"][0]["text"], "synthetic_temp explicit helper B")
        self.assertEqual(self.store.get_bundle(self.project, first["bundle_id"]), first)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        configs = {json.loads(row[0])["config_version"] for row in self.store.conn.execute("SELECT provider_json FROM jobs")}
        self.assertEqual(configs, {cfg1, cfg2})

    def test_positional_keyword_defaults_and_unrepresentable_values(self):
        _DEFAULT_CALLS.clear()

        def positional_factory(marker):
            @_provider
            def extractor(path, outdir, marker=marker, *, options=None):
                _DEFAULT_CALLS.append(marker)
                raw = raw_extract(path, outdir)
                raw["blocks"][0]["text"] = "synthetic_temp default " + marker
                return raw
            return extractor

        def keyword_factory(marker):
            @_provider
            def extractor(path, outdir, *, marker=marker, options=None):
                _DEFAULT_CALLS.append(marker)
                raw = raw_extract(path, outdir)
                raw["blocks"][0]["text"] = "synthetic_temp default " + marker
                return raw
            return extractor

        for factory in (positional_factory, keyword_factory):
            with self.subTest(factory=factory.__name__):
                a, b = factory("A"), factory("B")
                self.assertIs(a.__code__, b.__code__)
                self.assertIsNone(a.__closure__)
                self.assertIsNone(b.__closure__)
                self.assertEqual(_config_value(a), _config_value(b))
                first = self.store.ingest(self.project, [self.source], a)
                second = self.store.ingest(self.project, [self.source], b)
                self.assertNotEqual(first["bundle_id"], second["bundle_id"])
                self.assertEqual(first["blocks"][0]["text"], "synthetic_temp default A")
                self.assertEqual(second["blocks"][0]["text"], "synthetic_temp default B")
                replay = self.store.ingest(self.project, [self.source], b)
                self.assertEqual(second["bundle_id"], replay["bundle_id"])
        self.assertEqual(_DEFAULT_CALLS, ["A", "B", "A", "B"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 4)
        fresh_source = self.root / "invalid-default-not-registered.txt"
        fresh_source.write_text("synthetic_temp identity rejection", encoding="utf-8")
        before = self._counts()
        for invalid in (object(), raw_extract, {"callback": raw_extract}, float("nan")):
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaisesRegex(ContractError, "explicit config_version"):
                    self.store.ingest(self.project, [fresh_source], keyword_factory(invalid))
                self.assertEqual(self._counts(), before)
        # A deliberately explicit old configuration must not inspect defaults.
        prior_config = json.loads(self.store.conn.execute(
            "SELECT provider_json FROM jobs ORDER BY rowid DESC LIMIT 1").fetchone()[0])["config_version"]
        explicit = self.store.ingest(self.project, [self.source], keyword_factory(object()), config_version=prior_config)
        self.assertEqual(explicit["bundle_id"], second["bundle_id"])
        self.assertEqual(_DEFAULT_CALLS, ["A", "B", "A", "B"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 4)

    def test_disk_helper_change_requires_reload_without_side_effects(self):
        name = "synthetic_temp_reload_" + uuid.uuid4().hex
        path = self.root / (name + ".py")
        path.write_text(MODULE_SOURCE, encoding="utf-8")
        sys.path.insert(0, str(self.root))
        self.addCleanup(lambda: sys.path.remove(str(self.root)))
        module = importlib.import_module(name)
        self.addCleanup(lambda: sys.modules.pop(name, None))
        wrapper = module.extract
        identity = _config_value(wrapper)
        first = self.store.ingest(self.project, [self.source], wrapper)
        self.assertEqual(first["blocks"][0]["text"], "synthetic_temp helper A")
        self.assertEqual(module.calls, [1])
        path.write_text(MODULE_SOURCE.replace("synthetic_temp helper A", "synthetic_temp helper B"), encoding="utf-8")
        before = self._counts()
        with self.assertRaisesRegex(ContractError, "explicit config_version"):
            self.store.ingest(self.project, [self.source], wrapper)
        self.assertEqual(self._counts(), before)
        self.assertEqual(module.calls, [1], "disk-only changes must not run stale loaded code")
        module = importlib.reload(module)
        self.assertEqual(identity, _config_value(module.extract), "high-level wrapper code is unchanged")
        self.assertEqual(module.calls, [])
        second = self.store.ingest(self.project, [self.source], module.extract)
        self.assertEqual(module.calls, [1])
        self.assertEqual(second["blocks"][0]["text"], "synthetic_temp helper B")
        self.assertNotEqual(first["bundle_id"], second["bundle_id"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        self.assertEqual(len(self.store.list_bundles(self.project)), 2)
        self.assertEqual(self.store.get_bundle(self.project, first["bundle_id"]), first)


if __name__ == "__main__":
    unittest.main()
