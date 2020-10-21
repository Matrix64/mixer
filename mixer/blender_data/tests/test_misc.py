# GPLv3 License
#
# Copyright (C) 2020 Ubisoft
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import copy
from typing import Iterable, Set
import unittest

import bpy
from bpy import data as D  # noqa
from bpy import types as T  # noqa

from mixer.blender_data.aos_soa_proxy import SoaElement
from mixer.blender_data.blenddata import BlendData
from mixer.blender_data.bpy_data_proxy import BpyDataProxy
from mixer.blender_data.datablock_ref_proxy import DatablockRefProxy
from mixer.blender_data.filter import SynchronizedProperties, TypeFilterOut, test_properties, test_filter
from mixer.blender_data.tests.utils import test_blend_file


class TestLoadProxy(unittest.TestCase):
    # test_misc.TestLoadProxy
    def setUp(self):
        file = test_blend_file
        # file = r"D:\work\data\test_files\BlenderSS 2_82.blend"
        bpy.ops.wm.open_mainfile(filepath=file)
        self.proxy = BpyDataProxy()
        self.proxy.load(test_properties)

    def check(self, item, expected_elements):
        self.assertSetEqual(set(item._data.keys()), set(expected_elements))

    def expected_uuids(self, collection: bpy.types.Collection, names: Iterable[str]) -> Set[str]:
        return {collection[name].mixer_uuid for name in names}

    # @unittest.skip("")
    def test_blenddata(self):
        # test_misc.TestLoadProxy.test_blenddata
        blend_data = self.proxy._data
        expected_data = {"scenes", "collections", "objects", "materials", "lights"}
        self.assertTrue(all([e in blend_data.keys() for e in expected_data]))

        expected_uuids = self.expected_uuids(bpy.data.scenes, ["Scene_0", "Scene_1"])
        self.check(self.proxy._data["scenes"], expected_uuids)

        expected_uuids = self.expected_uuids(bpy.data.cameras, ["Camera_0", "Camera_1"])
        self.check(self.proxy._data["cameras"], expected_uuids)

        expected_uuids = self.expected_uuids(
            bpy.data.objects, ["Camera_obj_0", "Camera_obj_1", "Cone", "Cube", "Light"]
        )
        self.check(self.proxy._data["objects"], expected_uuids)

        expected_uuids = self.expected_uuids(
            bpy.data.collections, ["Collection_0_0", "Collection_0_1", "Collection_0_0_0", "Collection_1_0"]
        )
        self.check(self.proxy._data["collections"], expected_uuids)

    def test_blenddata_filtered(self):
        blend_data = self.proxy._data
        scene = blend_data["scenes"].search_one("Scene_0")._data
        self.assertTrue("eevee" in scene)

        filter_stack = copy.copy(test_filter)
        filter_stack.append({T.Scene: TypeFilterOut(T.SceneEEVEE)})
        proxy = BpyDataProxy()
        proxy.load(SynchronizedProperties(filter_stack))
        blend_data_ = proxy._data
        scene_ = blend_data_["scenes"].search_one("Scene_0")._data
        self.assertFalse("eevee" in scene_)

    # @unittest.skip("")
    def test_scene(self):
        # test_misc.TestLoadProxy.test_scene
        scene = self.proxy._data["scenes"].search_one("Scene_0")._data
        # will vary slightly during tuning of the default filter
        self.assertGreaterEqual(len(scene), 45)
        self.assertLessEqual(len(scene), 55)

        # objects = scene["objects"]._data
        # self.assertEqual(4, len(objects))

        # for o in objects.values():
        #     self.assertEqual(type(o), DatablockRefProxy, o)

        # builtin attributes (floats)
        frame_properties = [name for name in scene.keys() if name.startswith("frame_")]
        self.assertEqual(7, len(frame_properties))

        # bpy_struct
        eevee = scene["eevee"]._data
        self.assertEqual(58, len(eevee))

        # Currently mot loaded
        # # PropertiesGroup
        # cycles_proxy = scene["view_layers"]._data["View Layer"]._data["cycles"]
        # self.assertIsInstance(cycles_proxy, StructProxy)
        # self.assertEqual(32, len(cycles_proxy._data))

        # # The master collection
        # master_collection = scene["collection"]
        # self.assertIsInstance(master_collection, DatablockProxy)

    def test_collections(self):
        # test_misc.TestLoadProxy.test_collections
        collections = self.proxy._data["collections"]
        coll_0_0 = collections.search_one("Collection_0_0")._data

        coll_0_0_children = coll_0_0["children"]

        expected_uuids = self.expected_uuids(bpy.data.collections, ["Collection_0_0_0"])
        self.check(coll_0_0_children, expected_uuids)
        for c in coll_0_0_children._data.values():
            self.assertIsInstance(c, DatablockRefProxy)

        coll_0_0_objects = coll_0_0["objects"]
        expected_uuids = self.expected_uuids(bpy.data.objects, ["Camera_obj_0", "Camera_obj_1", "Cube", "Light"])
        self.check(coll_0_0_objects, expected_uuids)
        for o in coll_0_0_objects._data.values():
            self.assertIsInstance(o, DatablockRefProxy)

    def test_camera_focus_object_idref(self):
        # test_misc.TestLoadProxy.test_camera_focus_object_idref
        cam = D.cameras["Camera_0"]
        cam.dof.focus_object = D.objects["Cube"]
        self.proxy = BpyDataProxy()
        self.proxy.load(test_properties)
        # load into proxy
        cam_proxy = self.proxy.data("cameras").search_one("Camera_0")
        focus_object_proxy = cam_proxy.data("dof").data("focus_object")
        self.assertIsInstance(focus_object_proxy, DatablockRefProxy)
        self.assertEqual(focus_object_proxy._datablock_uuid, D.objects["Cube"].mixer_uuid)

    def test_camera_focus_object_none(self):
        # test_misc.TestLoadProxy.test_camera_focus_object_none
        self.proxy = BpyDataProxy()
        self.proxy.load(test_properties)
        # load into proxy
        cam_proxy = self.proxy.data("cameras").search_one("Camera_0")
        focus_object_proxy = cam_proxy.data("dof").data("focus_object")
        self.assertIs(focus_object_proxy, None)


class TestProperties(unittest.TestCase):
    def test_one(self):
        synchronized_properties = test_properties
        camera = D.cameras[0]

        # for 2.83.4
        expected_names = {
            "name",
            "name_full",
            "is_embedded_data",
            "type",
            "sensor_fit",
            "passepartout_alpha",
            "angle_x",
            "angle_y",
            "angle",
            "clip_start",
            "clip_end",
            "lens",
            "sensor_width",
            "sensor_height",
            "ortho_scale",
            "display_size",
            "shift_x",
            "shift_y",
            "stereo",
            "show_limits",
            "show_mist",
            "show_passepartout",
            "show_safe_areas",
            "show_safe_center",
            "show_name",
            "show_sensor",
            "show_background_images",
            "lens_unit",
            "show_composition_center",
            "show_composition_center_diagonal",
            "show_composition_thirds",
            "show_composition_golden",
            "show_composition_golden_tria_a",
            "show_composition_golden_tria_b",
            "show_composition_harmony_tri_a",
            "show_composition_harmony_tri_b",
            "dof",
            "background_images",
            "animation_data",
            "cycles",
        }
        names = {prop[0] for prop in synchronized_properties.properties(camera)}
        for name in expected_names:
            self.assertIn(name, names, "Expected list from 2.83.4, check version")


class TestBlendData(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.open_mainfile(filepath=test_blend_file)

    def test_one(self):
        blenddata = BlendData.instance()
        scenes = blenddata.collection("scenes").bpy_collection()
        sounds = blenddata.collection("sounds").bpy_collection()
        # identity is not true
        self.assertEqual(scenes, D.scenes)
        self.assertEqual(sounds, D.sounds)
        self.assertIs(scenes["Scene_0"], D.scenes["Scene_0"])

    def test_derived_from_id(self):
        light = bpy.data.lights.new("new_area", "AREA")
        blenddata = BlendData.instance()
        collection_name = blenddata.bl_collection_name_from_ID(type(light))
        self.assertEqual(collection_name, "lights")


class TestAosSoa(unittest.TestCase):
    def setUp(self):
        bpy.ops.wm.open_mainfile(filepath=test_blend_file)

    @unittest.skip("grease_pencil restricted to name")
    def test_all_soa_grease_pencil(self):
        import array

        bpy.ops.object.gpencil_add(type="STROKE")
        proxy = BpyDataProxy()
        proxy.load(test_properties)
        gp_layers = proxy.data("grease_pencils").search_one("Stroke").data("layers")
        gp_points = gp_layers.data("Lines").data("frames").data(0).data("strokes").data(0).data("points")._data
        expected = (
            ("co", array.array, "f"),
            ("pressure", array.array, "f"),
            ("strength", array.array, "f"),
            ("uv_factor", array.array, "f"),
            ("uv_rotation", array.array, "f"),
            ("select", list, bool),
        )
        for name, type_, element_type in expected:
            self.assertIn("co", gp_points)
            item = gp_points[name]
            self.assertIsInstance(item, SoaElement)
            self.assertIsInstance(item._data, type_)
            if type_ is array.array:
                self.assertEqual(item._data.typecode, element_type)
            else:
                self.assertIsInstance(item._data[0], element_type)

        self.assertEqual(len(gp_points["pressure"]._data), len(gp_points["strength"]._data))
        self.assertEqual(3 * len(gp_points["pressure"]._data), len(gp_points["co"]._data))
