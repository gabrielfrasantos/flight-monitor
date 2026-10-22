import re
from pathlib import Path

import flight_monitor as fm

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
MANIFEST = Path(__file__).resolve().parent.parent / ".release-please-manifest.json"


def test_script_version_matches_pyproject_and_release_manifest():
    match = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.MULTILINE)
    assert match is not None
    assert fm.__version__ == match.group(1)
    assert f'"{fm.__version__}"' in MANIFEST.read_text()


def test_version_flag(capsys):
    try:
        fm.parse_args(["--version"], settings=fm.DEFAULTS)
    except SystemExit as e:
        assert e.code == 0
    assert fm.__version__ in capsys.readouterr().out
