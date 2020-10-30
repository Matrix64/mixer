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
Proxies for collections of datablock (e.g. bpy.data.objects) or datablock references (e.g. Scene.objects)

See synchronization.md
"""
from __future__ import annotations

import functools
import logging
import traceback
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING, Union

import bpy
import bpy.types as T  # noqa

from mixer.blender_data.attributes import diff_attribute, read_attribute, write_attribute
from mixer.blender_data.blenddata import BlendData
from mixer.blender_data.changeset import Changeset, RenameChangeset
from mixer.blender_data.datablock_proxy import DatablockProxy
from mixer.blender_data.datablock_ref_proxy import DatablockRefProxy
from mixer.blender_data.diff import BpyPropCollectionDiff
from mixer.blender_data.filter import skip_bpy_data_item
from mixer.blender_data.proxy import DeltaUpdate, DeltaAddition, DeltaDeletion, MaxDepthExceeded
from mixer.blender_data.proxy import ensure_uuid, Proxy
from mixer.blender_data import specifics

if TYPE_CHECKING:
    from mixer.blender_data.proxy import Context


logger = logging.getLogger(__name__)


class DatablockCollectionProxy(Proxy):
    """
    Proxy to a bpy_prop_collection of standalone datablocks, i.e. of bpy.data collections

    This proxy keeps track of the state of the whole collection. The proxy contents are be instances
    of DatablockProxy.
    """

    def __init__(self, name: str):
        self._name: str = name
        # On item per datablock. The key is the uuid, which eases rename management
        self._data: Dict[str, DatablockProxy] = {}

    def __len__(self):
        return len(self._data)

    def reload_datablocks(self, datablocks: Dict[str, T.ID]):
        collection = getattr(bpy.data, self._name)
        datablocks.update({datablock.mixer_uuid: datablock for datablock in collection})

    def load(self, bl_collection: bpy.types.bpy_prop_collection, key: str, context: Context):  # noqa N802
        """
        Load bl_collection elements as standalone datablocks.
        """
        for name, item in bl_collection.items():
            collection_name = BlendData.instance().bl_collection_name_from_ID(item)
            if skip_bpy_data_item(collection_name, item):
                continue
            uuid = ensure_uuid(item)
            self._data[uuid] = DatablockProxy().load(item, name, context, bpy_data_collection_name=collection_name)

        return self

    def save(self, parent: Any, key: str, context: Context):
        """
        Save this Proxy a Blender collection that may be a collection of standalone datablocks in bpy.data
        or a collection of referenced datablocks like bpy.type.Collection.children
        """
        if not self._data:
            return

        target = getattr(parent, key, None)
        if target is None:
            # Don't log this, too many messages
            # f"Saving {self} into non existent attribute {bl_instance}.{attr_name} : ignored"
            return

        # collection of standalone datablocks
        for k, v in self._data.items():
            write_attribute(target, k, v, context)

    def find(self, key: str):
        return self._data.get(key)

    def create_datablock(
        self, incoming_proxy: DatablockProxy, context: Context
    ) -> Tuple[Optional[T.ID], Optional[RenameChangeset]]:
        """Create a bpy.data datablock from a received DatablockProxy and update the proxy structures accordingly

        Args:
            incoming_proxy : this proxy contents is used to update the bpy.data collection item
        """

        datablock, renames = incoming_proxy.create_standalone_datablock(context)

        if incoming_proxy.collection_name == "scenes":
            logger.warning(f"Creating scene '{incoming_proxy.data('name')}' uuid: '{incoming_proxy.mixer_uuid()}'")

            # One existing scene from the document loaded at join time could not be removed during clear_scene_conten().
            # Remove it now
            scenes = bpy.data.scenes
            if len(scenes) == 2 and ("_mixer_to_be_removed_" in scenes):
                from mixer.blender_client.scene import delete_scene

                scene_to_remove = scenes["_mixer_to_be_removed_"]
                logger.warning(
                    f"After create scene '{incoming_proxy.data('name')}' uuid: '{incoming_proxy.mixer_uuid()}''"
                )
                logger.warning(f"... delete {scene_to_remove} uuid '{scene_to_remove.mixer_uuid}'")
                delete_scene(scene_to_remove)

        if not datablock:
            return None, None

        uuid = incoming_proxy.mixer_uuid()
        self._data[uuid] = incoming_proxy
        context.proxy_state.datablocks[uuid] = datablock
        context.proxy_state.proxies[uuid] = incoming_proxy

        context.proxy_state.unresolved_refs.resolve(uuid, datablock)
        return datablock, renames

    def update_datablock(self, delta: DeltaUpdate, context: Context):
        """Update a bpy.data item from a received DatablockProxy and update the proxy state"""
        incoming_proxy = delta.value
        uuid = incoming_proxy.mixer_uuid()

        proxy: DatablockProxy = context.proxy_state.proxies.get(uuid)
        if proxy is None:
            logger.error(
                f"update_datablock(): Missing proxy for bpy.data.{incoming_proxy.collection_name}[{incoming_proxy.data('name')}] uuid {uuid}"
            )
            return

        if proxy.mixer_uuid() != incoming_proxy.mixer_uuid():
            logger.error(
                f"update_datablock : uuid mismatch between incoming {incoming_proxy.mixer_uuid()} ({incoming_proxy}) and existing {proxy.mixer_uuid} ({proxy})"
            )
            return

        # the ID will have changed if the object has been morphed (change light type, for instance)
        existing_id = context.proxy_state.datablocks.get(uuid)
        if existing_id is None:
            logger.warning(f"Non existent uuid {uuid} while updating {proxy.collection_name}[{proxy.data('name')}]")
            return None

        id_ = proxy.update_standalone_datablock(existing_id, delta, context)
        if existing_id != id_:
            # Not a problem for light morphing
            logger.warning(f"Update_datablock changes datablock {existing_id} to {id_}")
            context.proxy_state.datablocks[uuid] = id_

        return id_

    def remove_datablock(self, proxy: DatablockProxy, datablock: T.ID):
        """Remove a bpy.data collection item and update the proxy state"""
        logger.info("Perform removal for %s", proxy)
        try:
            if isinstance(datablock, T.Scene):
                from mixer.blender_client.scene import delete_scene

                delete_scene(datablock)
            else:
                proxy.collection.remove(datablock)
        except ReferenceError as e:
            # We probably have processed previously the deletion of a datablock referenced by Object.data (e.g. Light).
            # On both sides it deletes the Object as well. So the sender issues a message for object deletion
            # but deleting the light on this side has already deleted the object.
            # Alternatively we could try to sort messages on the sender side
            logger.warning(f"Exception during remove_datablock for {proxy}")
            logger.warning(f"... {e!r}")
        uuid = proxy.mixer_uuid()
        del self._data[uuid]

    def rename_datablock(self, proxy: DatablockProxy, new_name: str, datablock: T.ID):
        """
        Rename a bpy.data collection item and update the proxy state
        """
        logger.info("rename_datablock proxy %s datablock %s into %s", proxy, datablock, new_name)
        proxy.rename(new_name)
        datablock.name = new_name

    def update(self, diff: BpyPropCollectionDiff, context: Context) -> Changeset:
        """
        Update the proxy according to local datablock creations, removals or renames
        """
        changeset = Changeset()
        # Sort so that the tests receive the messages in deterministic order. Sad but not very harmfull
        added_names = sorted(diff.items_added.keys())
        for name in added_names:
            collection_name = diff.items_added[name]
            logger.info("Perform update/creation for %s[%s]", collection_name, name)
            try:
                # TODO could have a datablock directly
                collection = getattr(bpy.data, collection_name)
                id_ = collection.get(name)
                if id_ is None:
                    logger.error("update/ request addition for %s[%s] : not found", collection_name, name)
                    continue
                uuid = ensure_uuid(id_)
                context.proxy_state.datablocks[uuid] = id_
                proxy = DatablockProxy.make(id_).load(id_, name, context, bpy_data_collection_name=collection_name)
                context.proxy_state.proxies[uuid] = proxy
                self._data[uuid] = proxy
                changeset.creations.append(proxy)
            except MaxDepthExceeded as e:
                logger.error(f"MaxDepthExceeded while loading {collection_name}[{name}]:")
                logger.error("... Nested attribute depth is too large: ")
                logger.error(f"... {e!r}")
            except Exception:
                logger.error(f"Exception while loading {collection_name}[{name}]:")
                for line in traceback.format_exc().splitlines():
                    logger.error(line)

        for proxy in diff.items_removed:
            try:
                logger.info("Perform removal for %s", proxy)
                uuid = proxy.mixer_uuid()
                changeset.removals.append((uuid, proxy.collection_name, str(proxy)))
                del self._data[uuid]
                id_ = context.proxy_state.datablocks[uuid]
                del context.proxy_state.proxies[uuid]
                del context.proxy_state.datablocks[uuid]
            except Exception:
                logger.error(f"Exception during update/removed for proxy {proxy})  :")
                for line in traceback.format_exc().splitlines():
                    logger.error(line)

        #
        # Handle spontaneous renames
        #
        # - local and remote are initially synced with 2 objects with uuid/name D7/A FC/B
        # - local renames D7/A into B
        #   - D7 is actually renamed into B.001 !
        #   - we detect (D7 -> B.001)
        #   - remote proceses normally
        # - local renames D7/B.001 into B
        #   - D7 is renamed into B
        #   - FC is renamed into B.001
        #   - we detect (D7->B, FC->B.001)
        #   - local result is (D7/B, FC/B.001)
        # - local repeatedly renames the item named B.001 into B
        # - at some point on remote, the execution of a rename command will provoke a spontaneous rename,
        #   resulting in a situation where remote has FC/B.001 and D7/B.002 linked to the
        #   Master collection and also a FC/B unlinked
        #
        for proxy, new_name in diff.items_renamed:
            uuid = proxy.mixer_uuid()
            if proxy.collection[new_name] is not context.proxy_state.datablocks[uuid]:
                logger.error(
                    f"update rename : {proxy.collection}[{new_name}] is not {context.proxy_state.datablocks[uuid]} for {proxy}, {uuid}"
                )

            old_name = proxy.data("name")
            changeset.renames.append((proxy.mixer_uuid(), old_name, new_name, str(proxy)))
            proxy.rename(new_name)

        return changeset

    def search(self, name: str) -> List[DatablockProxy]:
        """Convenience method to find proxies by name instead of uuid (for tests only)"""
        results = []
        for uuid in self._data.keys():
            proxy_or_update = self.data(uuid)
            proxy = proxy_or_update if isinstance(proxy_or_update, Proxy) else proxy_or_update.value
            if proxy.data("name") == name:
                results.append(proxy)
        return results

    def search_one(self, name: str) -> DatablockProxy:
        """Convenience method to find a proxy by name instead of uuid (for tests only)"""
        results = self.search(name)
        return None if not results else results[0]


class DatablockRefCollectionProxy(Proxy):
    """
    Proxy to a bpy_prop_collection of datablock references like Scene.collection.objects or Object.materials


    TODO
    SceneObjects behave like dictionaries and IDMaterials bahave like sequence. Only the dictionary case
    is is correctly implemented.handles and having multiple items in IDMaterials is not supported
    """

    def __init__(self):
        # On item per datablock. The key is the uuid, which eases rename management
        self._data: Dict[str, DatablockRefProxy] = {}

    def __len__(self):
        return len(self._data)

    def load(self, bl_collection: bpy.types.bpy_prop_collection, key: Union[int, str], context: Context):  # noqa N802
        """
        Load bl_collection elements as references to bpy.data collections
        """

        # Warning:
        # - for Scene.object, can use uuids as no references are None
        # - for Mesh.matrials, more than one reference may be none, so cannot use a map, it is a sequence
        # Also, if Mesh.materials has a None item, len(bl_collection) == 2 (includes the None),
        # but len(bl_collection.items()) == 1 (excludes the None)
        # so iterate on bl_collection
        for item in bl_collection:
            if item is not None:
                proxy = DatablockRefProxy()
                uuid = item.mixer_uuid
                proxy.load(item, item.name, context)
                self._data[uuid] = proxy
            else:
                logger.error(f"unexpected None in {bl_collection}.{key}")
        return self

    def save(self, parent: Any, key: str, context: Context):
        """
        Save this Proxy a Blender collection that may be a collection of standalone datablocks in bpy.data
        or a collection of referenced datablocks like bpy.type.Collection.children
        """
        if not self._data:
            return

        collection = getattr(parent, key, None)
        if collection is None:
            # Don't log this, too many messages
            # f"Saving {self} into non existent attribute {bl_instance}.{attr_name} : ignored"
            return

        for _, ref_proxy in self._data.items():
            datablock = ref_proxy.target(context)
            if datablock:
                specifics.add_datablock_ref_element(collection, datablock)
            else:
                logger.info(f"unresolved reference {parent}.{key} -> {ref_proxy.display_string()}")
                add_element = functools.partial(specifics.add_datablock_ref_element, collection)
                context.proxy_state.unresolved_refs.append(ref_proxy.mixer_uuid, add_element)

    def apply(
        self,
        parent: Any,
        key: Union[int, str],
        collection_delta: Optional[DeltaUpdate],
        context: Context,
        to_blender: bool = True,
    ) -> DatablockRefCollectionProxy:
        """
        Apply a received Delta to this proxy.

        This method is only applicable if this is a proxy to a collection of datablock references
        like Scene.collection.objects, because changes to bpy.data are processed by xxx_datablock() methods.
        """

        collection_update: DatablockCollectionProxy = collection_delta.value
        assert type(collection_update) == type(self)
        collection = getattr(parent, key)
        for k, ref_delta in collection_update._data.items():
            try:
                if not isinstance(ref_delta, (DeltaAddition, DeltaDeletion)):
                    logger.warning(f"unexpected type for delta at {collection}[{k}]: {ref_delta}. Ignored")
                    continue
                ref_update: DatablockRefProxy = ref_delta.value
                if not isinstance(ref_update, DatablockRefProxy):
                    logger.warning(f"unexpected type for delta_value at {collection}[{k}]: {ref_update}. Ignored")
                    continue

                assert isinstance(ref_update, DatablockRefProxy)
                if to_blender:
                    uuid = ref_update._datablock_uuid
                    datablock = context.proxy_state.datablocks.get(uuid)
                    if isinstance(ref_delta, DeltaAddition):
                        if datablock is not None:
                            collection.link(datablock)
                        else:
                            logger.warning(
                                f"delta apply add for {parent}[{key}]: unregistered uuid {uuid} for {ref_update._debug_name}"
                            )

                    else:
                        if datablock is not None:
                            collection.unlink(datablock)
                        # else
                        #   we have already processed an Objet removal. Not an error

                if isinstance(ref_delta, DeltaAddition):
                    self._data[k] = ref_update
                else:
                    del self._data[k]
            except Exception as e:
                logger.warning(f"DatablockCollectionProxy.apply(). Processing {ref_delta} to_blender {to_blender}")
                logger.warning(f"... for {collection}[{k}]")
                logger.warning(f"... Exception: {e!r}")
                logger.warning("... Update ignored")
                continue

        return self

    def diff(
        self, collection: T.bpy_prop_collection, key: str, collection_property: T.Property, context: Context
    ) -> Optional[DeltaUpdate]:
        """
        Computes the difference between the state of an item tracked by this proxy and its Blender state.

        As this proxy tracks a collection, the result will be a DeltaUpdate that contains a DatablockCollectionProxy
        with an Delta item per added, deleted or update item

        Args:
            collection: the collection diff against this proxy
            collection_property: the property of collection in its enclosing object
        """

        # This method is called from the depsgraph handler. The proxy holds a representation of the Blender state
        # before the modification being processed. So the changeset is (Blender state - proxy state)

        # TODO how can this replace BpyBlendDiff ?

        diff = self.__class__()

        item_property = collection_property.fixed_type

        # keys are uuids
        proxy_keys = self._data.keys()
        blender_items = {item.mixer_uuid: item for item in collection.values()}
        blender_keys = blender_items.keys()
        added_keys = blender_keys - proxy_keys
        deleted_keys = proxy_keys - blender_keys
        maybe_updated_keys = proxy_keys & blender_keys

        for k in added_keys:
            value = read_attribute(blender_items[k], k, item_property, context)
            assert isinstance(value, (DatablockProxy, DatablockRefProxy))
            diff._data[k] = DeltaAddition(value)

        for k in deleted_keys:
            diff._data[k] = DeltaDeletion(self._data[k])

        for k in maybe_updated_keys:
            delta = diff_attribute(blender_items[k], k, item_property, self.data(k), context)
            if delta is not None:
                assert isinstance(delta, DeltaUpdate)
                diff._data[k] = delta

        if len(diff._data):
            return DeltaUpdate(diff)

        return None
