import copy
import json
import unittest

from elingpt.observation import decision_view, ObservationStream, ui_decision_view


class DecisionViewTests(unittest.TestCase):
    def fixture(self):
        return {"session_id": "s", "revision": 8, "state": {
            "tiles": [{"x": 1, "z": 2, "floor": 42, "block": 0, "blocked": False, "water": False}],
            "ui": {"format": "gameobject_tree", "roots": ["root"], "truncated": True, "active": True,
                   "input_busy": False, "drag_active": True, "nodes": [
                       {"id": "root", "name": "root", "visible": True, "actions": ["invoke_event"]},
                       {"id": "text", "parent_id": "root", "visible": True, "text": "Read this dialogue",
                        "label": "Read this dialogue", "actions": [], "events": ["large metadata"]},
                       {"id": "button", "parent_id": "root", "name": "CustomUnknownPanel", "visible": True,
                        "label": "Accept", "actions": ["click", "invoke_event"], "components": ["CustomControl"]},
                       {"id": "hidden", "visible": False, "text": "hidden"},
                       {"id": "password", "visible": True, "sensitive": True, "text": "secret", "value": "secret"},
                   ]}}}

    def test_preserves_decisions_without_mutating_input(self):
        source = self.fixture()
        original = copy.deepcopy(source)
        view = decision_view(source)
        self.assertEqual(source, original)
        self.assertEqual(view["revision"], 8)
        self.assertTrue(view["state"]["ui"]["truncated"])
        self.assertTrue(view["state"]["ui"]["drag_active"])
        nodes = view["state"]["ui"]["nodes"]
        self.assertEqual([node["id"] for node in nodes], ["text", "button"])
        self.assertEqual(nodes[0]["label"], "Read this dialogue")
        self.assertEqual(view["state"]["ui"]["action_sets"][nodes[1]["action_set"]], ["click", "invoke_event"])
        self.assertNotIn("secret", json.dumps(view))

    def test_terrain_is_lossless(self):
        source = self.fixture()
        terrain = decision_view(source)["state"]["terrain"]
        self.assertEqual([dict(zip(terrain["columns"], cell)) for cell in terrain["cells"]],
                         source["state"]["tiles"])

    def test_native_cell_variants_and_future_fields_are_preserved(self):
        source = self.fixture()
        source["state"]["tiles"][0].update(
            floor_variant=3, block_variant=2, block_type="TileTypeWall",
            block_axis=None, has_door=True, object=7, object_variant=5,
            future_variant={"value": 9})
        source["state"]["placement"] = {"direction": 2, "can_rotate": True}
        original = copy.deepcopy(source)
        view = decision_view(source)
        terrain = view["state"]["terrain"]
        self.assertEqual(dict(zip(terrain["columns"], terrain["cells"][0])), source["state"]["tiles"][0])
        self.assertEqual(view["state"]["placement"], source["state"]["placement"])
        self.assertEqual(source, original)

    def test_local_terrain_window_preserves_all_seven_walls(self):
        source = self.fixture()
        walls = {(49, 47), (49, 48), (49, 49), (50, 47), (51, 47), (51, 48), (51, 49)}
        source["state"]["limits"] = {"radius": 6, "terrain_visibility_filtered": False}
        source["state"]["tiles"] = [
            {"x": x, "z": z, "floor": 42, "block": 144 if (x, z) in walls else 0,
             "blocked": (x, z) in walls, "water": False,
             "block_variant": 0, "block_axis": "x" if (x, z) in walls else None}
            for x in range(46, 59) for z in range(39, 52)]
        stream = ObservationStream()
        baseline = stream.present(source, full=True)
        terrain = baseline["state"]["terrain"]
        self.assertEqual(len(terrain["cells"]), 169)
        tiles = [dict(zip(terrain["columns"], row)) for row in terrain["cells"]]
        self.assertEqual({(tile["x"], tile["z"]) for tile in tiles if tile["block"] == 144}, walls)
        self.assertFalse(baseline["state"]["limits"]["terrain_visibility_filtered"])

        cleared = copy.deepcopy(source)
        cleared["revision"] += 1
        wall = next(tile for tile in cleared["state"]["tiles"] if (tile["x"], tile["z"]) == (49, 48))
        wall.update(block=0, blocked=False, block_axis=None)
        patch = stream.present(cleared)["terrain_delta"]
        self.assertEqual(patch["removed"], [])
        self.assertEqual([dict(zip(patch["columns"], row)) for row in patch["upsert"]], [wall])

    def test_variant_only_change_appears_in_terrain_delta(self):
        source = self.fixture()
        source["state"]["tiles"] = [
            {"x": x, "z": 0, "floor": 42, "block": 8, "blocked": True, "water": False,
             "block_variant": 0, "floor_variant": 0, "object_variant": 0}
            for x in range(50)]
        for field in ("block_variant", "floor_variant", "object_variant"):
            with self.subTest(field=field):
                stream = ObservationStream()
                baseline = stream.present(source)
                changed = copy.deepcopy(source)
                changed["revision"] += 1
                changed["state"]["tiles"][2][field] = 3
                patch = stream.present(changed)["terrain_delta"]
                self.assertEqual(patch["columns"], baseline["state"]["terrain"]["columns"])
                self.assertEqual(len(patch["upsert"]), 1)
                self.assertEqual(dict(zip(patch["columns"], patch["upsert"][0])), changed["state"]["tiles"][2])

    def test_changed_terrain_schema_replaces_instead_of_incompatible_delta(self):
        source = self.fixture()
        source["state"]["inventory"] = [{"name": "unchanged item"}] * 40
        stream = ObservationStream()
        stream.present(source)
        updated = copy.deepcopy(source)
        updated["revision"] += 1
        updated["state"]["tiles"][0]["block_variant"] = 1
        delta = stream.present(updated)
        self.assertNotIn("terrain_delta", delta)
        self.assertEqual(delta["state_delta"]["terrain"], decision_view(updated)["state"]["terrain"])

    def test_visible_ground_objects_survive_compaction_and_deltas(self):
        source = self.fixture()
        source["state"]["ground_objects"] = [{"x": 1, "z": 2, "id": 7, "name": "vine"}]
        source["state"]["inventory"] = [{"name": "unchanged item"}] * 40
        stream = ObservationStream()
        baseline = stream.present(source)
        self.assertEqual(baseline["state"]["ground_objects"], source["state"]["ground_objects"])
        harvested = copy.deepcopy(source)
        harvested["revision"] += 1
        harvested["state"]["ground_objects"] = []
        delta = stream.present(harvested)
        self.assertTrue(delta["delta"])
        self.assertEqual(delta["state_delta"]["ground_objects"], [])

    def test_action_status_uses_same_projection(self):
        source = {"action": {"status": "completed"}, "observation": self.fixture(), "quitting": False}
        view = decision_view(source)
        self.assertEqual(view["action"], source["action"])
        self.assertEqual(view["observation"], decision_view(source["observation"]))
        self.assertFalse(view["quitting"])

    def test_full_subtree_and_non_observations_are_unchanged(self):
        subtree = {"revision": 8, "ui": self.fixture()["state"]["ui"]}
        self.assertIs(decision_view(subtree), subtree)
        result = {"action": {"action_id": "a"}}
        self.assertIs(decision_view(result), result)

    def test_large_tree_is_bounded_and_omissions_are_explicit(self):
        source = self.fixture()
        source["state"]["ui"]["nodes"] += [
            {"id": str(i), "parent_id": "root", "role": "button", "visible": True,
             "label": "An available generic control", "actions": ["click", "right_click", "hover"]}
            for i in range(1000)]
        view = decision_view(source)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode()), 16000)
        self.assertGreater(view["state"]["ui"]["summary_omitted"], 0)
        self.assertEqual(view["state"]["ui"]["tree_node_count"], 1005)

    def test_dense_terrain_does_not_starve_ui_decisions(self):
        source = self.fixture()
        source["state"]["tiles"] = [
            {"x": x, "z": z, "floor": 42, "block": 144, "blocked": True, "water": False,
             "block_axis": "x", "block_variant": 0, "block_type": "TileTypeWall",
             "block_shape": {"full_wall": False, "cross_wall": False,
                             "toggle_pillar": False, "hide_pillar": False},
             "floor_variant": 0, "has_door": False, "object": 0, "object_variant": 0}
            for x in range(13) for z in range(13)]
        source["state"]["ui"]["nodes"] += [
            {"id": str(i), "visible": True, "label": "A HUD control", "actions": ["click"]}
            for i in range(100)]
        original = copy.deepcopy(source)
        view = decision_view(source)
        terrain = view["state"]["terrain"]
        self.assertEqual([dict(zip(terrain["columns"], cell)) for cell in terrain["cells"]],
                         source["state"]["tiles"])
        ui = view["state"]["ui"]
        self.assertTrue({"text", "button"} <= {node["id"] for node in ui["nodes"]})
        self.assertGreater(ui["summary_omitted"], 0)
        self.assertLessEqual(len(json.dumps(ui["nodes"], separators=(",", ":")).encode()), 4002)
        self.assertEqual(source, original)

    def test_modal_text_and_controls_outrank_hud_regardless_of_text_length(self):
        source = self.fixture()
        source["state"]["ui"]["nodes"] = [
            {"id": str(i), "path": "UI\\Widget Manager\\Stats", "visible": True,
             "text": "A long HUD message which must not displace the current decision.",
             "label": "A long HUD message which must not displace the current decision.",
             "actions": ["click", "drag"]}
            for i in range(1000)]
        source["state"]["ui"]["nodes"] += [
            {"id": "title", "path": "UI\\Layers\\UnknownModPanel\\Title", "text": "Torch", "label": "Torch"},
            {"id": "cost", "path": "UI\\Layers\\UnknownModPanel\\Cost", "text": "3", "label": "3"},
            {"id": "craft", "path": "UI\\Layers\\UnknownModPanel\\Action", "label": "Craft", "actions": ["click"]},
            {"id": "tip", "path": "UI\\ToolTips\\Note", "text": "Need a branch", "label": "Need a branch"},
            {"id": "item", "path": "UI\\Layers(Float)\\CustomInventory\\Item", "label": "Item", "actions": ["hover"]},
        ]
        view = decision_view(source)
        ids = {node["id"] for node in view["state"]["ui"]["nodes"]}
        self.assertTrue({"title", "cost", "craft", "tip", "item"} <= ids)
        self.assertGreater(view["state"]["ui"]["summary_omitted"], 0)
        self.assertLessEqual(len(json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode()), 16000)

    def test_opt_in_ui_decisions_preserve_revision_and_raw_tree(self):
        raw = {"revision": 8, "ui": self.fixture()["state"]["ui"]}
        original = copy.deepcopy(raw)
        compact = ui_decision_view(raw)
        self.assertEqual(compact["revision"], 8)
        self.assertEqual(compact["ui"]["format"], "decision_nodes")
        self.assertEqual([node["id"] for node in compact["ui"]["nodes"]], ["text", "button"])
        self.assertEqual(raw, original)

    def test_terrain_deltas_reconstruct_values_removals_insertions_and_order(self):
        source = self.fixture()
        source["state"]["tiles"] = [
            {"x": x, "z": z, "floor": 42, "block": 0, "blocked": False, "water": False}
            for x in range(9) for z in range(9)]
        for change in ("update", "replace", "reorder"):
            with self.subTest(change=change):
                stream = ObservationStream()
                baseline = stream.present(source)
                updated = copy.deepcopy(source)
                updated["revision"] += 1
                tiles = updated["state"]["tiles"]
                if change == "update":
                    tiles[3]["blocked"] = True
                elif change == "replace":
                    tiles.pop(0)
                    tiles.insert(2, {"x": 10, "z": 0, "floor": 44, "block": 0, "blocked": False, "water": True})
                else:
                    tiles.reverse()
                delta = stream.present(updated)
                self.assertTrue(delta["delta"])
                self.assertNotIn("terrain", delta["state_delta"])
                patch = delta["terrain_delta"]
                cells = {tuple(cell[:2]): cell for cell in baseline["state"]["terrain"]["cells"]}
                for key in patch["removed"]:
                    del cells[tuple(key)]
                for cell in patch["upsert"]:
                    cells[tuple(cell[:2])] = cell
                order = [tuple(key) for key in patch.get("order", list(cells))]
                restored = {"columns": patch["columns"], "cells": [cells[key] for key in order]}
                self.assertEqual(restored, decision_view(updated)["state"]["terrain"])
                self.assertLess(len(json.dumps(patch)), len(json.dumps(restored)))

    def test_stream_delta_reconstructs_current_state(self):
        source = self.fixture()
        source["state"]["inventory"] = [{"name": "unchanged item"}] * 40
        stream = ObservationStream()
        baseline = stream.present(source, full=True)
        updated = copy.deepcopy(source)
        updated["revision"] = 9
        updated["state"]["held_item"] = None
        updated["state"]["ui"]["nodes"][1]["label"] = "Next dialogue"
        updated["state"]["ui"]["nodes"][1]["text"] = "Next dialogue"
        updated["state"]["ui"]["nodes"].pop(2)
        expected = decision_view(updated)
        delta = stream.present(updated)
        self.assertTrue(delta["delta"])
        self.assertEqual(delta["base_revision"], 8)
        self.assertNotIn("inventory", delta["state_delta"])
        restored = copy.deepcopy(baseline)
        restored.update({key: value for key, value in delta.items()
                         if key not in {"delta", "base_revision", "state_delta", "state_removed", "ui_delta"}})
        restored["state"].update(delta["state_delta"])
        for key in delta["state_removed"]:
            restored["state"].pop(key, None)
        nodes = {node["id"]: node for node in restored["state"]["ui"]["nodes"]}
        for key in delta["ui_delta"]["removed"]:
            nodes.pop(key)
        for node in delta["ui_delta"]["upsert"]:
            nodes[node["id"]] = node
        order = delta["ui_delta"].get("order", list(nodes))
        restored["state"]["ui"] = {**delta["ui_delta"]["meta"], "nodes": [nodes[key] for key in order]}
        self.assertEqual(restored, expected)
        self.assertEqual(stream.present(updated, full=True), expected)

    def test_stream_resets_on_session_or_generation_change(self):
        source = self.fixture()
        stream = ObservationStream()
        self.assertNotIn("delta", stream.present(source))
        changed = copy.deepcopy(source)
        changed["session_id"] = "new"
        self.assertNotIn("delta", stream.present(changed))
        changed["generation"] = "new-zone"
        self.assertNotIn("delta", stream.present(changed))
