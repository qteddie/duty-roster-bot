"""The server's image runs the Python the Dockerfile names (conda ``python=3.11``): every module must parse there.

Newer syntax passes every test on a newer Python and then keeps the bot from starting at all: an f-string nested in
the same quotes (PEP 701, 3.12+) did exactly that, 「SyntaxError: f-string: expecting '}'」 on the server.
``ast.parse(feature_version=...)`` does not catch it, so the files are parsed by that Python itself: this one when
the tests run on it, else a ``python3.11`` on the PATH; without either the test is skipped.
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARSE = """import ast, sys
bad = []
for name in sys.argv[1:]:
    try:
        ast.parse(open(name, encoding="utf-8").read(), name)
    except SyntaxError as exc:
        bad.append(f"{name}:{exc.lineno}: {exc.msg}")
print("\\n".join(bad))
sys.exit(1 if bad else 0)
"""


def server_python() -> tuple[int, int]:
    """(major, minor) of the Python the Dockerfile's conda environment is made with."""
    found = re.search(r"python=(\d+)\.(\d+)", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    return int(found.group(1)), int(found.group(2))


def sources() -> list[str]:
    """Every Python file of the project, not those under hidden folders (.git, .claude worktrees) or caches."""
    return sorted(str(path) for path in ROOT.rglob("*.py")
                  if not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(ROOT).parts))


class ServerPythonTests(unittest.TestCase):
    def test_every_module_parses_on_the_servers_python(self):
        version = server_python()
        interpreter = sys.executable if sys.version_info[:2] == version else shutil.which("python%d.%d" % version)
        if not interpreter:
            self.skipTest("沒有 Python %d.%d 可以檢查（伺服器用的版本）" % version)
        files = sources()
        self.assertIn(str(ROOT / "all_staff" / "bot.py"), files)
        result = subprocess.run([interpreter, "-c", PARSE, *files], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, "伺服器的 Python %d.%d 讀不懂：\n" % version + result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
