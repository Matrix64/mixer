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

"""
Proxy helpers Blender types that have different interfaces or requirements, but do not require their own complete
Proxy implementation.


TODO Enhance this module so that it is possible to reference types that do not exist in all Blender versions or
to control behavior with plugin data.
"""

from __future__ import annotations

import array
import logging
from pathlib import Path
import traceback
from typing import Any, Dict, ItemsView, Optional, TYPE_CHECKING, Union

from mixer.local_data import get_resolved_file_path

import bpy
import bpy.types as T  # noqa N812
import bpy.path
import mathutils


if TYPE_CHECKING:
    from mixer.blender_data.aos_proxy import AosProxy
    from mixer.blender_data.proxy import Context, Proxy
    from mixer.blender_data.bpy_data_proxy import VisitState
    from mixer.blender_data.datablock_proxy import DatablockProxy
    from mixer.blender_data.struct_collection_proxy import StructCollectionProxy

logger = logging.getLogger(__name__)


# Beware that MeshVertex must be handled as SOA although "groups" is a variable length item.
# Enums are not handled by foreach_get()
soable_collection_properties = {
    T.GPencilStroke.bl_rna.properties["points"],
    T.GPencilStroke.bl_rna.properties["triangles"],
    T.Mesh.bl_rna.properties["edges"],
    T.Mesh.bl_rna.properties["face_maps"],
    T.Mesh.bl_rna.properties["loops"],
    T.Mesh.bl_rna.properties["loop_triangles"],
    # messy: :MeshPolygon.vertices has variable length, not 3 as stated in the doc, so ignore
    T.Mesh.bl_rna.properties["polygons"],
    T.Mesh.bl_rna.properties["polygon_layers_float"],
    T.Mesh.bl_rna.properties["polygon_layers_int"],
    T.Mesh.bl_rna.properties["vertices"],
    T.MeshUVLoopLayer.bl_rna.properties["data"],
    T.MeshLoopColorLayer.bl_rna.properties["data"],
}

# in sync with soa_initializers
soable_properties = (
    T.BoolProperty,
    T.IntProperty,
    T.FloatProperty,
    mathutils.Vector,
    mathutils.Color,
    mathutils.Quaternion,
)

# in sync with soable_properties
soa_initializers: Dict[type, array.array] = {
    bool: array.array("b", [0]),
    int: array.array("l", [0]),
    float: array.array("f", [0.0]),
    mathutils.Vector: array.array("f", [0.0]),
    mathutils.Color: array.array("f", [0.0]),
    mathutils.Quaternion: array.array("f", [0.0]),
}


def is_soable_collection(prop):
    return prop in soable_collection_properties


def is_soable_property(bl_rna_property):
    return isinstance(bl_rna_property, soable_properties)


node_tree_type = {
    "SHADER": "ShaderNodeTree",
    "COMPOSITOR": "CompositorNodeTree",
    "TEXTURE": "TextureNodeTree",
}


def bpy_data_ctor(collection_name: str, proxy: DatablockProxy, context: Any) -> Optional[T.ID]:
    """
    Create an element in a bpy.data collection.

    Contains collection-specific code is the mathod to add an element is not new(name: str)
    """
    collection = getattr(bpy.data, collection_name)
    if collection_name == "images":
        image = None
        image_name = proxy.data("name")
        filepath = proxy.data("filepath")
        resolved_filepath = get_resolved_file_path(filepath)
        packed_files = proxy.data("packed_files")
        if packed_files is not None and packed_files.length:
            name = proxy.data("name")
            width, height = proxy.data("size")
            try:
                with open(resolved_filepath, "rb") as image_file:
                    buffer = image_file.read()
                image = collection.new(name, width, height)
                image.pack(data=buffer, data_len=len(buffer))
            except RuntimeError as e:
                logger.warning(
                    f'Cannot load packed image original "{filepath}"", resolved "{resolved_filepath}". Exception: '
                )
                logger.warning(f"... {e}")
                return None

        else:
            try:
                image = collection.load(resolved_filepath)
                image.name = image_name
            except RuntimeError as e:
                logger.warning(f'Cannot load image original "{filepath}"", resolved "{resolved_filepath}". Exception: ')
                logger.warning(f"... {e}")
                return None

        # prevent filepath to be overwritten by the incoming proxy value as it would attempt to reload the file
        # from the incoming path that may not exist
        proxy._data["filepath"] = resolved_filepath
        proxy._data["filepath_raw"] = resolved_filepath
        return image

    if collection_name == "objects":
        name = proxy.data("name")
        target = None
        target_proxy = proxy.data("data")
        if target_proxy is not None:
            # use class name to work around circular references with proxy.py
            target_proxy_class = target_proxy.__class__.__name__
            if target_proxy_class != "DatablockRefProxy":
                # error on the sender side
                logger.warning(
                    f"bpy.data.objects[{name}].data proxy is a {target_proxy_class}. Expected a DatablockRefProxy"
                )
                logger.warning("... loaded as Empty")
            else:
                target = target_proxy.target(context)
        object_ = collection.new(name, target)
        return object_

    if collection_name == "lights":
        name = proxy.data("name")
        light_type = proxy.data("type")
        light = collection.new(name, light_type)
        return light

    if collection_name == "node_groups":
        name = proxy.data("name")
        type_ = node_tree_type[proxy.data("type")]
        return collection.new(name, type_)

    if collection_name == "sounds":
        filepath = proxy.data("filepath")
        # TODO what about "check_existing" ?
        id_ = collection.load(filepath)
        # we may have received an ID named xxx.001 although filepath is xxx, so fix it now
        id_.name = proxy.data("name")

        return id_

    name = proxy.data("name")
    try:
        id_ = collection.new(name)
    except TypeError as e:
        logger.error(f"Exception while calling : bpy.data.{collection_name}.new({name})")
        logger.error(f"TypeError : {e}")
        return None

    return id_


filter_crop_transform = [
    T.EffectSequence,
    T.ImageSequence,
    T.MaskSequence,
    T.MetaSequence,
    T.MovieClipSequence,
    T.MovieSequence,
    T.SceneSequence,
]


def conditional_properties(bpy_struct: T.Struct, properties: ItemsView) -> ItemsView:
    """Filter properties list according to a specific property value in the same ID

    This prevents loading values that cannot always be saved, such as Object.instance_collection
    that can only be saved when Object.data is None

    Args:
        properties: the properties list to filter
    Returns:

    """
    if isinstance(bpy_struct, T.ColorManagedViewSettings):
        if bpy_struct.use_curve_mapping:
            # Empty
            return properties
        filtered = {}
        filter_props = ["curve_mapping"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.Object):
        if not bpy_struct.data:
            # Empty
            return properties
        filtered = {}
        filter_props = ["instance_collection"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.Mesh):
        if not bpy_struct.use_auto_texspace:
            # Empty
            return properties
        filtered = {}
        filter_props = ["texspace_location", "texspace_size"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.MetaBall):
        if not bpy_struct.use_auto_texspace:
            return properties
        filter_props = ["texspace_location", "texspace_size"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.Node):
        if bpy_struct.hide:
            return properties

        # not hidden: saving width_hidden is ignored
        filter_props = ["width_hidden"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    if isinstance(bpy_struct, T.NodeTree):
        if not bpy_struct.is_embedded_data:
            return properties

        filter_props = ["name"]
        filtered = {k: v for k, v in properties if k not in filter_props}
        return filtered.items()

    filter_props = []
    if any(isinstance(bpy_struct, t) for t in filter_crop_transform):
        if not bpy_struct.use_crop:
            filter_props.append("crop")
        if not bpy_struct.use_translation:
            filter_props.append("transform")

    if not filter_props:
        return properties
    filtered = {k: v for k, v in properties if k not in filter_props}
    return filtered.items()

    return properties


def pre_save_id(proxy: DatablockProxy, target: T.ID, context: Context) -> T.ID:
    """Process attributes that must be saved first and return a possibly updated reference to the target"""
    if isinstance(target, bpy.types.Material):
        use_nodes = proxy.data("use_nodes")
        if use_nodes:
            target.use_nodes = True

        is_grease_pencil = proxy.data("is_grease_pencil")
        # will be None for a DeltaUpdate that does not modify "is_grease_pencil"
        if is_grease_pencil is not None:
            # is_grease_pencil is modified
            if is_grease_pencil:
                if not target.grease_pencil:
                    bpy.data.materials.create_gpencil_data(target)
            else:
                if target.grease_pencil:
                    bpy.data.materials.remove_gpencil_data(target)
    elif isinstance(target, T.Scene):
        # Set 'use_node' to True first is the only way I know to be able to set the 'node_tree' attribute
        use_nodes = proxy.data("use_nodes")
        if use_nodes:
            target.use_nodes = True
        sequence_editor = proxy.data("sequence_editor")
        if sequence_editor is not None and target.sequence_editor is None:
            target.sequence_editor_create()
    elif isinstance(target, T.Light):
        # required first to have access to new light type attributes
        light_type = proxy.data("type")
        if light_type is not None and light_type != target.type:
            target.type = light_type
            # must reload the reference
            target = proxy.target(context)
    elif isinstance(target, T.ColorManagedViewSettings):
        use_curve_mapping = proxy.data("use_curve_mapping")
        if use_curve_mapping:
            target.use_curve_mapping = True
    elif isinstance(target, bpy.types.World):
        use_nodes = proxy.data("use_nodes")
        if use_nodes:
            target.use_nodes = True
    elif isinstance(target, T.Mesh):
        context.visit_state.funcs["Mesh.clear_geometry"] = target.clear_geometry

    return target


def pre_save_struct(proxy: Proxy, bpy_struct: T.Struct, attr_name: str):
    """Process attributes that must be saved first"""
    target = getattr(bpy_struct, attr_name, None)
    if target is None:
        return None
    if isinstance(target, T.ColorManagedViewSettings):
        use_curve_mapping = proxy.data("use_curve_mapping")
        if use_curve_mapping:
            target.use_curve_mapping = True


def post_save_id(proxy: Proxy, bpy_id: T.ID):
    """Apply type specific patches after loading bpy_struct into proxy"""
    pass


_link_collections = tuple(type(t.bl_rna) for t in [T.CollectionObjects, T.CollectionChildren, T.SceneObjects])


def add_datablock_ref_element(collection: T.bpy_prop_collection, datablock: T.ID):
    """Add an element to a bpy_prop_collection using the collection specific API"""
    bl_rna = getattr(collection, "bl_rna", None)
    if bl_rna is not None:
        if isinstance(bl_rna, _link_collections):
            collection.link(datablock)
            return

        if isinstance(bl_rna, type(T.IDMaterials.bl_rna)):
            collection.append(datablock)
            return

    logging.warning(f"add_datablock_ref_element : no implementation for {collection} ")


non_effect_sequences = {"IMAGE", "SOUND", "META", "SCENE", "MOVIE", "MOVIECLIP", "MASK"}
effect_sequences = set(T.EffectSequence.bl_rna.properties["type"].enum_items.keys()) - non_effect_sequences

_new_name_types = (type(T.UVLoopLayers.bl_rna), type(T.LoopColors.bl_rna), type(T.FaceMaps.bl_rna))


def add_element(proxy: Proxy, collection: T.bpy_prop_collection, key: str, context: Context):
    """Add an element to a bpy_prop_collection using the collection specific API"""

    bl_rna = getattr(collection, "bl_rna", None)
    if bl_rna is not None:
        if isinstance(bl_rna, (type(T.ObjectModifiers.bl_rna), type(T.ObjectGpencilModifiers.bl_rna))):
            name = proxy.data("name")
            modifier_type = proxy.data("type")
            return collection.new(name, modifier_type)

        if isinstance(bl_rna, type(T.SequenceModifiers.bl_rna)):
            name = proxy.data("name")
            type_ = proxy.data("type")
            return collection.new(name, type_)

        if isinstance(bl_rna, type(T.ObjectConstraints.bl_rna)):
            type_ = proxy.data("type")
            return collection.new(type_)

        if isinstance(bl_rna, type(T.Nodes.bl_rna)):
            node_type = proxy.data("bl_idname")
            return collection.new(node_type)

        if isinstance(bl_rna, _new_name_types):
            name = proxy.data("name")
            return collection.new(name=name)

        if isinstance(bl_rna, type(T.GreasePencilLayers.bl_rna)):
            name = proxy.data("info")
            return collection.new(name)

        if isinstance(bl_rna, type(T.KeyingSets.bl_rna)):
            idname = proxy.data("bl_idname")
            return collection.new(name=key, idname=idname)

        if isinstance(bl_rna, type(T.KeyingSetPaths.bl_rna)):
            # TODO current implementation fails
            # All keying sets paths have an empty name, and insertion with add() fails
            # with an empty name
            target_ref = proxy.data("id")
            if target_ref is None:
                target = None
            else:
                target = target_ref.target(context)
            data_path = proxy.data("data_path")
            index = proxy.data("array_index")
            group_method = proxy.data("group_method")
            group_name = proxy.data("group")
            return collection.add(
                target_id=target, data_path=data_path, index=index, group_method=group_method, group_name=group_name
            )

        if isinstance(bl_rna, type(T.Nodes.bl_rna)):
            node_type = proxy.data("bl_idname")
            return collection.new(node_type)

        if isinstance(
            bl_rna,
            (
                type(T.NodeInputs.bl_rna),
                type(T.NodeOutputs.bl_rna),
                type(T.NodeTreeInputs.bl_rna),
                type(T.NodeTreeOutputs.bl_rna),
            ),
        ):
            socket_type = proxy.data("type")
            name = proxy.data("name")
            return collection.new(socket_type, name)

        if isinstance(bl_rna, type(T.Sequences.bl_rna)):
            type_ = proxy.data("type")
            name = proxy.data("name")
            channel = proxy.data("channel")
            frame_start = proxy.data("frame_start")
            if type_ in effect_sequences:
                # overwritten anyway
                frame_end = frame_start + 1
                return collection.new_effect(name, type_, channel, frame_start, frame_end=frame_end)
            if type_ == "SOUND":
                sound = proxy.data("sound")
                target = sound.target(context)
                if not target:
                    logger.warning(f"missing target ID block for bpy.data.{sound.collection}[{sound.key}] ")
                    return None
                filepath = target.filepath
                return collection.new_sound(name, filepath, channel, frame_start)
            if type_ == "MOVIE":
                filepath = proxy.data("filepath")
                return collection.new_movie(name, filepath, channel, frame_start)
            if type_ == "IMAGE":
                directory = proxy.data("directory")
                filename = proxy.data("elements").data(0).data("filename")
                filepath = str(Path(directory) / filename)
                return collection.new_image(name, filepath, channel, frame_start)

            logger.warning(f"Sequence type not implemented: {type_}")
            # SCENE may be harder than it seems, since we cannot order scene creations.
            # Currently the creation order is the "deepmost" order as listed in proxy.py:_creation_order
            # but it does not work for this case
            return None

    try:
        return collection.add()
    except Exception:
        pass

    # try our best
    new_or_add = getattr(collection, "new", None)
    if new_or_add is None:
        new_or_add = getattr(collection, "add", None)
    if new_or_add is None:
        logger.warning(f"Not implemented new or add for bpy.data.{collection}[{key}] ...")
        return None
    try:
        return new_or_add(key)
    except Exception:
        logger.warning(f"Not implemented new or add for type {type(collection)} for {collection}[{key}] ...")
        for s in traceback.format_exc().splitlines():
            logger.warning(f"...{s}")
        return None


always_clear = [type(T.ObjectModifiers.bl_rna), type(T.ObjectGpencilModifiers.bl_rna), type(T.SequenceModifiers.bl_rna)]
"""Collections in this list are order dependent and should always be cleared"""

_resize_geometry_types = tuple(type(t.bl_rna) for t in [T.MeshEdges, T.MeshLoops, T.MeshVertices, T.MeshPolygons])


def resize_geometry(mesh_geometry_item: T.Property, proxy: AosProxy, visit_state: VisitState):
    """
    Args:
        mesh_geometry_item : Mesh.vertices, Mesh.loops, ...
        proxy : proxy to mesh_geometry_item
    """
    existing_length = len(mesh_geometry_item)
    incoming_length = proxy.length
    if existing_length != incoming_length:
        if existing_length != 0:
            logger.debug(f"resize_geometry(): calling clear() for {mesh_geometry_item}")
            visit_state.funcs["Mesh.clear_geometry"]()
            # We should need it only once for a whole Mesh
            del visit_state.funcs["Mesh.clear_geometry"]
        logger.debug(f"resize_geometry(): add({incoming_length}) for {mesh_geometry_item}")
        mesh_geometry_item.add(incoming_length)


def truncate_collection(target: T.bpy_prop_collection, proxy: Union[StructCollectionProxy, AosProxy], context: Context):
    """
    TODO check if this is obsolete since Delta updates
    """
    if not hasattr(target, "bl_rna"):
        return

    target_rna = target.bl_rna
    if any(isinstance(target_rna, t) for t in always_clear):
        target.clear()
        return

    if isinstance(target_rna, _resize_geometry_types):
        resize_geometry(target, proxy, context.visit_state)
        return

    if isinstance(target_rna, type(T.GPencilStrokePoints.bl_rna)):
        existing_length = len(target)
        incoming_length = proxy.length
        delta = incoming_length - existing_length
        if delta > 0:
            target.add(delta)
        else:
            while delta < 0:
                target.pop()
                delta += 1
        return

    incoming_keys = set(proxy._data.keys())
    existing_keys = set(target.keys())
    truncate_keys = existing_keys - incoming_keys
    if not truncate_keys:
        return
    if isinstance(target_rna, type(T.KeyingSets.bl_rna)):
        for k in truncate_keys:
            target.active_index = target.find(k)
            bpy.ops.anim.keying_set_remove()
    else:
        try:
            for k in truncate_keys:
                target.remove(target[k])
        except Exception:
            logger.warning(f"Not implemented truncate_collection for type {target.bl_rna} for {target} ...")
            for s in traceback.format_exc().splitlines():
                logger.warning(f"...{s}")
