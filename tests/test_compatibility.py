"""Checks for startup on platforms without Unix account APIs."""

import subprocess
import sys
import unittest


class CompatibilityTest(unittest.TestCase):
    def test_module_imports_without_pwd(self):
        script = '''
try:
    import builtins
except ImportError:
    import __builtin__ as builtins

original_import = builtins.__import__

def import_without_pwd(name, *args, **kwargs):
    if name == 'pwd':
        raise ImportError('pwd is unavailable on this platform')
    return original_import(name, *args, **kwargs)

builtins.__import__ = import_without_pwd
from bepasty_cli import cli
assert cli.shorten_homepath('/example/home') == '/example/home'
'''
        subprocess.check_call([sys.executable, '-c', script])


if __name__ == '__main__':
    unittest.main()
