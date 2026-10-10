from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_smoke import LiveChecks


class LiveTreeChecksTests(unittest.TestCase):
    def setUp(self):
        self.tree = {
            "format": "gameobject_tree",
            "roots": ["root"],
            "nodes": [
                {"id": "root", "parent_id": None, "depth": 0, "sensitive": False,
                 "visible": True, "interactable": True, "actions": []},
                {"id": "child", "parent_id": "root", "depth": 1, "sensitive": False,
                 "visible": True, "interactable": True, "actions": ["click"]},
            ],
        }

    def test_unique_forest(self):
        LiveChecks.check_tree(self.tree)

    def test_rejects_overlapping_roots_and_duplicate_nodes(self):
        duplicate = copy.deepcopy(self.tree)
        duplicate["nodes"].append(copy.deepcopy(duplicate["nodes"][1]))
        overlapping = copy.deepcopy(self.tree)
        overlapping["roots"].append("root")
        for tree in [duplicate, overlapping]:
            with self.subTest(tree=tree), self.assertRaises(AssertionError):
                LiveChecks.check_tree(tree)

    def test_rejects_broken_parent_depth(self):
        for parent, depth in [("missing", 1), ("root", 0), (None, 1)]:
            tree = copy.deepcopy(self.tree)
            tree["nodes"][1].update(parent_id=parent, depth=depth)
            with self.subTest(parent=parent, depth=depth), self.assertRaises(AssertionError):
                LiveChecks.check_tree(tree)

    def test_rejects_hidden_or_sensitive_actions(self):
        for fields in [{"visible": False}, {"interactable": False},
                       {"sensitive": True, "text": None}]:
            tree = copy.deepcopy(self.tree)
            tree["nodes"][1].update(fields)
            with self.subTest(fields=fields), self.assertRaises(AssertionError):
                LiveChecks.check_tree(tree)

    def test_accepts_redacted_sensitive_node(self):
        self.tree["nodes"][1].update(sensitive=True, actions=[], text=None)
        LiveChecks.check_tree(self.tree)
        self.tree["nodes"][1]["value"] = "must not be exposed"
        with self.assertRaises(AssertionError):
            LiveChecks.check_tree(self.tree)
