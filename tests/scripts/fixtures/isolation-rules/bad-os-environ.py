# Fixture: Python test file that directly modifies os.environ
# Expected: triggers no-direct-os-environ violations

import os


def test_direct_assignment():
    os.environ["MY_KEY"] = "value"
    assert os.environ["MY_KEY"] == "value"


def test_setdefault():
    os.environ.setdefault("OTHER_KEY", "default")
    assert os.environ.get("OTHER_KEY") == "default"


def test_update():
    os.environ.update({"BATCH_KEY": "batch_value"})
    assert os.environ.get("BATCH_KEY") == "batch_value"
