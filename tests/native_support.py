"""Native test fixtures; independent of the agent inspection tools."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class CoreCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import binaryninja as bn
        except ImportError:
            raise unittest.SkipTest('Requires Binary Ninja Python API') from None
        cls.bn = bn
        initial = bn.load(b'\x00', update_analysis=False)
        initial.file.close()

    def setUp(self):
        root = Path(__file__).resolve().parents[1] / '.test-artifacts/native-fixtures'
        root.mkdir(parents=True, exist_ok=True)
        temporary = TemporaryDirectory(prefix='case-', dir=root)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self._closed_views = set()

    def binary(self):
        view = self.bn.BinaryView.new(bytes.fromhex('85ff7409e81700000083c02ac331c0c3')
                                      + b'\x90' * 16 + bytes.fromhex('8d4701c3'))
        self.addCleanup(view.file.close)
        view.platform = self.bn.Platform['linux-x86_64']
        view.add_function(0)
        view.add_function(0x20)
        view.update_analysis_and_wait()
        view.get_function_at(0).type = 'int compute_result(int objectId)'
        view.get_function_at(0x20).name = 'process'
        view.update_analysis_and_wait()
        return view

    def load(self, file):
        view = self.bn.load(file, update_analysis=False)
        self.addCleanup(self.close_view, view)
        return view

    def close_view(self, view):
        if id(view) not in self._closed_views:
            self._closed_views.add(id(view))
            view.file.close()
