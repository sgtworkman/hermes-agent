#!/usr/bin/env python3
"""Read-only validation of recorded browser accessibility and viewport evidence."""
import hashlib
import json
from pathlib import Path
import sys


def verify(path):
    record = json.loads(path.read_text())
    labels = ["Low", "Medium", "Extra High"]
    for viewport in ("desktop", "mobile"):
        state = record[viewport]
        rows = state["rows"]
        assert [r["label"] for r in rows] == labels
        assert [r["checked"] for r in rows] == ["false", "false", "true"]
        size = state["viewport"]
        for row in rows:
            rect = row["rect"]
            assert rect["width"] > 0 and rect["height"] >= 20
            assert 0 <= rect["x"] <= size["width"] - rect["width"]
            assert 0 <= rect["y"] <= size["height"] - rect["height"]
    assert record["mobile"]["viewport"]["width"] == 375
    selection = record["afterLowSelection"]
    assert selection["storedEffort"] == "low"
    assert [r["label"] for r in selection["rows"]] == labels
    assert [r["checked"] for r in selection["rows"]] == ["true", "false", "false"]
    assert record["source_anchors"]
    for anchor in record["source_anchors"]:
        assert hashlib.sha256(Path(anchor["path"]).read_bytes()).hexdigest() == anchor["sha256"]
    print(json.dumps({"status": "PASS", "viewports": ["desktop", "mobile"],
                      "mobile_width": 375, "selected_effort": "low"}))


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
