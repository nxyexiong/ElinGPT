import unittest

from elingpt.guide import gameplay_guide, gameplay_instructions
from elingpt.observation import _size
from elingpt.tools import TOOL_MAP


class GameplayGuideTests(unittest.TestCase):
    def test_native_cardinal_axes_and_isometric_projection(self):
        axes = gameplay_guide()["coordinates"]
        self.assertEqual(axes["north"], [0, 1])
        self.assertEqual(axes["south"], [0, -1])
        self.assertEqual(axes["east"], [1, 0])
        self.assertEqual(axes["west"], [-1, 0])
        projection = lambda x, z: (x + z, z - x)
        self.assertEqual(projection(*axes["north"]), (1, 1))
        self.assertEqual(projection(*axes["east"]), (1, -1))
        self.assertEqual(projection(-1, 1), (0, 2))
        self.assertEqual(projection(-1, -1), (-2, 0))

    def test_guide_is_small_independent_of_live_state_and_defensive(self):
        first = gameplay_guide()
        first["coordinates"]["north"][1] = -1
        self.assertEqual(gameplay_guide()["coordinates"]["north"], [0, 1])
        self.assertLess(_size(gameplay_guide()), 5000)
        text = gameplay_instructions()
        for concept in ("equipped=true", "held_item", "drag_active", "full backpack",
                        "awaiting_ui", "recipe", "overworld", "cardinal", "stamina"):
            self.assertIn(concept.lower(), text.lower())

    def test_tool_arguments_explain_axes_without_extra_observation_payload(self):
        schema = TOOL_MAP["elin_act"]["inputSchema"]["properties"]
        self.assertIn("+1 north", schema["dz"]["description"])
        self.assertIn("-1 west", schema["dx"]["description"])

    def test_corner_mechanism_distinguishes_held_and_existing_rotation(self):
        text = gameplay_instructions()
        for rule in ("2 both segments in a FIXED orientation", "0->1->2->0",
                     "full_wall is a separate", "toggles cross_wall",
                     "rotate_existing_block", "rotate_held changes only", "Do NOT set all four corners to 2",
                     "southeast (max x,min z) uses 2", "southwest uses 1", "northeast uses 0", "isWallEdge"):
            self.assertIn(rule, text)

    def test_rectangle_requires_a_fixed_corner_and_an_automatic_edge(self):
        variants = {(49, 47): 1, (50, 47): 0, (51, 47): 2,
                    (49, 48): 1, (51, 48): 1,
                    (50, 49): 0, (51, 49): 0}
        northwest = (49, 49)
        east = (northwest[0] + 1, northwest[1])
        south = (northwest[0], northwest[1] - 1)
        self.assertNotIn(northwest, variants)
        self.assertIn(variants[east], (0, 2))
        self.assertIn(variants[south], (1, 2))
        self.assertEqual(sum(variant == 2 for variant in variants.values()), 1)
        self.assertIn("wall_edge=true", gameplay_instructions())
