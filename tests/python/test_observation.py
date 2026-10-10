import copy
import io
import json
import random
import unittest

from elingpt.client import BridgeError
from elingpt.observation import (
    ObservationStream, _size, apply_observation, compact_observation, decision_view,
    pack_records, pack_terrain, ui_decision_view, unpack_records, unpack_terrain,
)
from elingpt.telemetry import Metrics


class DecisionViewTests(unittest.TestCase):
    def fixture(self):
        return {"session_id": "s", "generation": "g", "revision": 8, "status": "playing", "ready": True,
                "state": {
            "tiles": [{"x": x, "z": z, "floor": 42, "block": 0, "blocked": False, "water": False,
                       "block_variant": 0, "floor_variant": (x + z) % 4,
                       "block_axis": None, "block_shape": None, "object_variant": 0}
                      for x in range(13) for z in range(13)],
            "inventory": [{"handle": f"item:{i:02}", "name": f"Item {i}", "kind": "item",
                           "location": "inventory", "count": 1, "equipped": i < 2,
                           "position": {"x": i, "z": 0}} for i in range(37)],
            "entities": [{"handle": f"entity:{i:02}", "name": f"NPC {i}", "hostile": False,
                          "position": {"x": i, "z": 0}} for i in range(20)],
            "ground_objects": [{"x": i, "z": 1, "id": 7, "name": "vine"} for i in range(20)],
            "messages": [{"index": i, "text": f"Message {i}"} for i in range(20)],
            "ui": {"format": "gameobject_tree", "roots": ["root"], "truncated": False, "active": True,
                   "input_busy": False, "drag_active": True, "nodes": [
                       {"id": "root", "name": "root", "visible": True, "actions": ["invoke_event"]},
                       {"id": "text", "parent_id": "root", "visible": True, "text": "Read this dialogue",
                        "label": "Read this dialogue", "actions": [], "events": ["large metadata"]},
                       {"id": "button", "parent_id": "root", "name": "CustomUnknownPanel", "visible": True,
                        "label": "Accept", "actions": ["click", "invoke_event"], "components": ["CustomControl"]},
                       {"id": "hidden", "visible": False, "text": "hidden"},
                       {"id": "password", "visible": True, "sensitive": True, "text": "secret", "value": "secret"},
                   ]}}}

    def assert_reconstructed(self, restored, raw):
        expected = decision_view(raw)
        actual = {key: value for key, value in restored.items() if key not in {"view_version", "observation_id"}}
        self.assertEqual(actual, expected)

    def test_projection_preserves_decisions_and_does_not_mutate_input(self):
        source = self.fixture()
        original = copy.deepcopy(source)
        view = decision_view(source)
        self.assertEqual(source, original)
        ui = view["state"]["ui"]
        self.assertTrue(ui["drag_active"])
        self.assertEqual([node["id"] for node in ui["nodes"]], ["text", "button"])
        self.assertEqual(ui["nodes"][0]["label"], "Read this dialogue")
        self.assertEqual(ui["action_sets"][ui["nodes"][1]["action_set"]], ["click", "invoke_event"])
        self.assertNotIn("secret", json.dumps(view))
        self.assertNotIn("events", ui["nodes"][0])

    def test_nonpausing_processing_window_flags_survive_projection(self):
        source = self.fixture()
        source["state"]["ui"].update(pauses_game=False, processing=True)
        stream = ObservationStream()
        previous = apply_observation(None, stream.present(source))
        self.assertTrue(previous["state"]["ui"]["processing"])
        self.assertFalse(previous["state"]["ui"]["pauses_game"])
        source["state"]["ui"]["processing"] = False
        restored = apply_observation(previous, stream.present(source))
        self.assertFalse(restored["state"]["ui"]["processing"])
        self.assertTrue(restored["state"]["ui"]["active"])

    def test_lossless_grid_and_inventory_save_measurable_bytes(self):
        source = self.fixture()
        canonical = decision_view(source)
        packed = compact_observation(canonical)
        restored = apply_observation(None, packed)
        self.assert_reconstructed(restored, source)
        self.assertEqual(packed["state"]["terrain"]["format"], "palette_grid")
        self.assertEqual(packed["state"]["inventory"]["format"], "table")
        self.assertLess(_size(packed), _size(canonical) * 0.6)
        self.assertEqual(len(restored["state"]["terrain"]["cells"]), 169)
        self.assertEqual(sum(item["equipped"] for item in restored["state"]["inventory"]), 2)

    def test_sparse_empty_negative_and_single_cell_terrain(self):
        for tiles in ([], [{"x": -2, "z": -3, "floor": 1}],
                      [{"x": -10, "z": -10, "floor": 1}, {"x": 1000, "z": 1000, "floor": 2}]):
            with self.subTest(tiles=tiles):
                source = self.fixture()
                source["state"]["tiles"] = tiles
                terrain = decision_view(source)["state"]["terrain"]
                self.assertEqual(unpack_terrain(pack_terrain(terrain)), terrain)

    def test_grid_holes_and_nested_future_fields_roundtrip(self):
        source = self.fixture()
        source["state"]["tiles"].pop(7)
        for tile in source["state"]["tiles"]:
            tile.update(future={"variant": tile["x"] % 2}, has_door=tile["x"] == 1)
        terrain = decision_view(source)["state"]["terrain"]
        packed = pack_terrain(terrain)
        self.assertIn(None, [code for row in packed["rows"] for code in row])
        self.assertEqual(unpack_terrain(packed), terrain)

    def test_duplicate_terrain_coordinates_are_rejected(self):
        terrain = decision_view(self.fixture())["state"]["terrain"]
        terrain["cells"].append(terrain["cells"][0])
        with self.assertRaisesRegex(BridgeError, "Duplicate terrain"):
            pack_terrain(terrain)

    def test_records_distinguish_missing_null_false_and_nested_values(self):
        items = [{"handle": f"item:{i}", "shared": {"value": False}, "optional": None}
                 for i in range(100)]
        del items[0]["optional"]
        items[1]["optional"] = False
        packed = pack_records(items)
        self.assertEqual(packed["format"], "table")
        self.assertEqual(unpack_records(packed), items)
        self.assertEqual(unpack_records(pack_records([])), [])

    def test_numeric_and_boolean_types_are_not_collapsed(self):
        items = [{"name": "same", "nested": {"value": value}} for value in ([True, 1, 1.0] * 50)]
        self.assertEqual(json.dumps(unpack_records(pack_records(items)), sort_keys=True),
                         json.dumps(items, sort_keys=True))
        source = self.fixture()
        source["state"]["tiles"][0]["future"] = True
        source["state"]["tiles"][1]["future"] = 1
        source["state"]["inventory"][0]["nested"] = {"value": 1}
        stream = ObservationStream()
        previous = apply_observation(None, stream.present(source))
        source["state"]["inventory"][0]["nested"]["value"] = True
        source["state"]["tiles"][0]["future"] = 1
        restored = apply_observation(previous, stream.present(source))
        self.assertIs(type(restored["state"]["inventory"][0]["nested"]["value"]), bool)
        terrain = restored["state"]["terrain"]
        self.assertIs(type(terrain["cells"][0][terrain["columns"].index("future")]), int)
        self.assertEqual(json.dumps(restored["state"], sort_keys=True),
                         json.dumps(decision_view(source)["state"], sort_keys=True))

    def test_all_seven_walls_orientation_and_wall_removal_survive(self):
        source = self.fixture()
        walls = {(1, 1), (1, 2), (1, 3), (2, 1), (3, 1), (3, 2), (3, 3)}
        source["state"]["limits"] = {"radius": 6, "terrain_visibility_filtered": False}
        for tile in source["state"]["tiles"]:
            if (tile["x"], tile["z"]) in walls:
                tile.update(block=144, blocked=True, block_variant=1, block_axis="z",
                            block_shape={"full_wall": True, "hide_pillar": False})
        stream = ObservationStream()
        restored = apply_observation(None, stream.present(source))
        terrain = restored["state"]["terrain"]
        tiles = [dict(zip(terrain["columns"], row)) for row in terrain["cells"]]
        self.assertEqual({(tile["x"], tile["z"]) for tile in tiles if tile["block"] == 144}, walls)
        self.assertFalse(restored["state"]["limits"]["terrain_visibility_filtered"])
        changed = copy.deepcopy(source)
        wall = next(tile for tile in changed["state"]["tiles"] if (tile["x"], tile["z"]) == (1, 2))
        wall.update(block=0, blocked=False, block_axis=None, block_shape=None)
        changed["revision"] += 1
        delta = stream.present(changed)
        self.assertEqual(delta["terrain_delta"]["removed"], [])
        self.assertEqual(len(delta["terrain_delta"]["upsert"]), 1)
        self.assert_reconstructed(apply_observation(restored, delta), changed)

    def test_automatic_corner_remains_blocked_without_a_block(self):
        source = self.fixture()
        for tile in source["state"]["tiles"]:
            tile["wall_edge"] = False
        stream = ObservationStream()
        previous = apply_observation(None, stream.present(source))
        corner = source["state"]["tiles"][0]
        corner.update(block=0, blocked=True, wall_edge=True)
        delta = stream.present(source)
        restored = apply_observation(previous, delta)
        terrain = restored["state"]["terrain"]
        tile = dict(zip(terrain["columns"], terrain["cells"][0]))
        self.assertEqual(tile["block"], 0)
        self.assertTrue(tile["blocked"])
        self.assertTrue(tile["wall_edge"])
        self.assert_reconstructed(restored, source)

    def test_unchanged_response_is_small_and_has_no_repeated_ui_metadata(self):
        source = self.fixture()
        stream = ObservationStream()
        baseline = stream.present(source)
        delta = stream.present(source)
        self.assertTrue(delta["delta"])
        self.assertLess(_size(delta), 200)
        self.assertLess(_size(delta), _size(baseline) * 0.05)
        self.assertNotIn("ui_delta", delta)
        self.assertNotIn("state_delta", delta)
        self.assert_reconstructed(apply_observation(apply_observation(None, baseline), delta), source)

    def test_cursor_recovery_and_forced_full_are_explicit(self):
        source = self.fixture()
        stream = ObservationStream()
        first = stream.present(source)
        second = stream.present(source, since=first["observation_id"])
        self.assertTrue(second["delta"])
        self.assertNotIn("delta", stream.present(source, since=first["observation_id"]))
        self.assertNotIn("delta", stream.present(source, full=True))
        for previous in (None, {"observation_id": "wrong", "revision": source["revision"]}):
            with self.assertRaisesRegex(BridgeError, "missing_baseline"):
                apply_observation(previous, second)
        restarted = ObservationStream().present(source)
        self.assertNotEqual(first["observation_id"], restarted["observation_id"])

    def test_session_and_generation_changes_reset_baseline(self):
        source = self.fixture()
        stream = ObservationStream()
        stream.present(source)
        for field in ("session_id", "generation"):
            source[field] = "new"
            full = stream.present(source)
            self.assertNotIn("delta", full)
            self.assert_reconstructed(apply_observation(None, full), source)

    def test_component_deltas_preserve_changes_deletions_and_new_records(self):
        source = self.fixture()
        for name, field, value in (("inventory", "count", 3), ("entities", "hostile", True),
                                   ("ground_objects", "name", "harvested"),
                                   ("messages", "text", "updated message")):
            with self.subTest(name=name):
                stream = ObservationStream()
                baseline = apply_observation(None, stream.present(source))
                changed = copy.deepcopy(source)
                changed["revision"] += 1
                changed["state"][name][3][field] = value
                changed["state"][name].pop(0)
                delta = stream.present(changed)
                self.assertIn(name, delta["collections_delta"])
                self.assertEqual(len(unpack_records(delta["collections_delta"][name]["upsert"])), 1)
                self.assert_reconstructed(apply_observation(baseline, delta), changed)

    def test_ground_object_clear_and_log_window_updates(self):
        source = self.fixture()
        stream = ObservationStream()
        baseline = apply_observation(None, stream.present(source))
        changed = copy.deepcopy(source)
        changed["revision"] += 1
        changed["state"]["ground_objects"] = []
        changed["state"]["messages"] = changed["state"]["messages"][1:] + [{"index": 20, "text": "New message"}]
        delta = stream.present(changed)
        self.assertEqual(delta["collections_delta"]["messages"]["removed"], [0])
        self.assert_reconstructed(apply_observation(baseline, delta), changed)

    def test_unkeyed_and_duplicate_records_are_not_silently_deduplicated(self):
        source = self.fixture()
        for items in ([{"name": "unknown"}] * 40, [{"handle": "same", "count": 1}] * 40):
            source["state"]["inventory"] = items
            stream = ObservationStream()
            restored = apply_observation(None, stream.present(source))
            changed = copy.deepcopy(source)
            changed["state"]["inventory"].pop()
            patch = stream.present(changed)
            self.assert_reconstructed(apply_observation(restored, patch), changed)

    def test_variant_only_and_schema_changes_are_preserved(self):
        source = self.fixture()
        for field in ("block_variant", "floor_variant", "object_variant", "new_variant"):
            with self.subTest(field=field):
                stream = ObservationStream()
                restored = apply_observation(None, stream.present(source))
                changed = copy.deepcopy(source)
                changed["state"]["tiles"][5][field] = 3
                delta = stream.present(changed)
                if field == "new_variant":
                    self.assertNotIn("terrain_delta", delta)
                else:
                    self.assertEqual(len(delta["terrain_delta"]["upsert"]), 1)
                self.assert_reconstructed(apply_observation(restored, delta), changed)

    def test_walk_window_delta_does_not_send_coordinate_order(self):
        source = self.fixture()
        stream = ObservationStream()
        restored = apply_observation(None, stream.present(source))
        changed = copy.deepcopy(source)
        for tile in changed["state"]["tiles"]:
            tile["x"] += 1
        changed["state"]["tiles"].reverse()
        delta = stream.present(changed)
        self.assertNotIn("order", delta.get("terrain_delta", {}))
        self.assert_reconstructed(apply_observation(restored, delta), changed)

    def test_ui_deltas_merge_metadata_and_preserve_node_order_and_removals(self):
        source = self.fixture()
        stream = ObservationStream()
        restored = apply_observation(None, stream.present(source))
        changed = copy.deepcopy(source)
        changed["state"]["ui"]["nodes"].reverse()
        changed["state"]["ui"]["nodes"] = [node for node in changed["state"]["ui"]["nodes"] if node["id"] != "button"]
        changed["state"]["ui"]["focus_id"] = "text"
        changed["state"]["held_item"] = None
        changed["revision"] += 1
        delta = stream.present(changed)
        self.assertNotIn("roots", delta["ui_delta"]["meta"])
        self.assert_reconstructed(apply_observation(restored, delta), changed)
        del changed["state"]["ui"]["focus_id"]
        del changed["state"]["held_item"]
        del changed["ready"]
        delta2 = stream.present(changed)
        self.assertIn("focus_id", delta2["ui_delta"]["meta_removed"])
        self.assert_reconstructed(apply_observation(apply_observation(restored, delta), delta2), changed)

    def test_new_null_metadata_is_not_confused_with_an_absent_field(self):
        source = self.fixture()
        stream = ObservationStream()
        previous = apply_observation(None, stream.present(source))
        source["new_metadata"] = None
        source["state"]["ui"]["focus_id"] = None
        delta = stream.present(source)
        self.assertIn("new_metadata", delta)
        self.assertIn("focus_id", delta["ui_delta"]["meta"])
        self.assert_reconstructed(apply_observation(previous, delta), source)

    def test_paged_ui_has_exact_text_and_reachable_continuation(self):
        ui = self.fixture()["state"]["ui"]
        ui["nodes"] += [{"id": str(i), "label": f"Control {i}", "visible": True, "actions": ["click"]}
                        for i in range(100)]
        raw = {"revision": 8, "ui": ui}
        original = copy.deepcopy(raw)
        offset, page_id, ids = 0, None, []
        while True:
            result = ui_decision_view(raw, offset=offset, page_id=page_id, max_bytes=1024)
            self.assertLessEqual(_size(result), 1024)
            page = result["ui"]
            ids += [node["id"] for node in page["nodes"]]
            if page["next_offset"] is None:
                break
            offset, page_id = page["next_offset"], page["page_id"]
        self.assertEqual(ids, ["text", "button"] + [str(i) for i in range(100)])
        self.assertEqual(raw, original)
        with self.assertRaisesRegex(BridgeError, "stale_ui_page"):
            ui_decision_view({**raw, "revision": 9}, offset=offset, page_id=page_id)
        with self.assertRaisesRegex(BridgeError, "stale_ui_page"):
            ui_decision_view(raw, offset=1)

    def test_modal_and_tooltip_nodes_precede_hud_without_hardcoded_screens(self):
        ui = self.fixture()["state"]["ui"]
        ui["nodes"] = [{"id": str(i), "path": "UI\\Widget Manager\\Stats", "text": "HUD text " * 5}
                       for i in range(100)]
        for name, path in (("title", "UI\\Layers\\CustomPanel"), ("cost", "UI\\Layers\\CustomPanel"),
                           ("tip", "UI\\ToolTips\\Any"), ("menu", "UI\\ContextMenu\\Any"),
                           ("item", "UI\\Layers(Float)\\Any")):
            ui["nodes"].append({"id": name, "path": path, "text": name, "actions": ["click"]})
        result = ui_decision_view({"revision": 1, "ui": ui}, max_bytes=2000)["ui"]
        self.assertEqual([node["id"] for node in result["nodes"][:5]], ["title", "cost", "tip", "menu", "item"])
        self.assertGreater(result["summary_omitted"], 0)

    def test_oversize_dialogue_is_explicit_and_not_lost(self):
        ui = {"nodes": [{"id": "long", "text": "Exact dialogue " * 1000, "visible": True},
                        {"id": "next", "text": "Next"}]}
        page = ui_decision_view({"revision": 1, "ui": ui}, max_bytes=1024)["ui"]
        self.assertTrue(page["budget_exceeded"])
        self.assertEqual(page["nodes"][0]["text"], ui["nodes"][0]["text"])
        self.assertEqual(page["next_offset"], 1)

    def test_full_budget_overflow_is_explicit_without_dropping_state(self):
        source = self.fixture()
        source["state"]["inventory"][0]["description"] = "Unique item description " * 2000
        packed = compact_observation(decision_view(source))
        self.assertTrue(packed["budget"]["exceeded"])
        self.assert_reconstructed(apply_observation(None, packed), source)

    def test_action_wrapper_and_non_observations(self):
        stream = ObservationStream()
        raw = self.fixture()
        value = {"action": {"status": "completed"}, "observation": raw, "quitting": False}
        result = stream.present(value)
        self.assertEqual(result["action"], value["action"])
        self.assert_reconstructed(apply_observation(None, result["observation"]), raw)
        prior = stream.previous
        self.assertEqual(stream.present({"revision": 9, "ui": {}}), {"revision": 9, "ui": {}})
        self.assertIs(stream.previous, prior)

    def test_randomized_multi_step_reconstruction(self):
        rng = random.Random(734)
        source = self.fixture()
        stream = ObservationStream()
        restored = None
        for step in range(80):
            source["revision"] += 1
            source["state"]["tiles"][rng.randrange(169)]["object_variant"] = rng.randrange(8)
            source["state"]["inventory"][rng.randrange(37)]["count"] = rng.randrange(1, 10)
            source["state"]["messages"] = source["state"]["messages"][1:] + [
                {"index": 20 + step, "text": f"Event {step}"}]
            source["state"]["ui"]["drag_active"] = bool(step % 2)
            wire = stream.present(source, full=step % 17 == 0)
            # JSON roundtrip also exercises tuple-valued ground-object keys.
            restored = apply_observation(restored, json.loads(json.dumps(wire)))
            self.assert_reconstructed(restored, source)

    def test_metrics_record_sizes_and_usage_without_payloads(self):
        sink = io.StringIO()
        metrics = Metrics(sink)
        value = {"state": {"player": {"name": "private game text"}}}
        metrics.record("observe", value, usage={"prompt_tokens": 12, "completion_tokens": 3,
                                               "secret": "must not be logged"})
        record = json.loads(sink.getvalue())
        self.assertEqual(record["bytes"], _size(value))
        self.assertEqual(record["usage"], {"prompt_tokens": 12, "completion_tokens": 3})
        self.assertNotIn("private game text", sink.getvalue())
        self.assertNotIn("secret", sink.getvalue())
