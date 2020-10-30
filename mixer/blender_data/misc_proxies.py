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
Utility proxy classes

See synchronization.md
"""
from __future__ import annotations

import logging
from typing import Any, Optional, TYPE_CHECKING, Union

import bpy.types as T  # noqa

from mixer.blender_data.attributes import read_attribute
from mixer.blender_data.proxy import DeltaUpdate, Proxy
from mixer.blender_data.datablock_ref_proxy import DatablockRefProxy

if TYPE_CHECKING:
    from mixer.blender_data.proxy import Context

logger = logging.getLogger(__name__)


class NonePtrProxy(Proxy):
    """Proxy for a None PointerProperty value.

    When setting a PointerProperty from None to a valid reference, apply_attributs requires that
    the proxyfied value implements apply().

    This is used for Pointers to standalone datablocks like Scene.camera.

    TODO Check it it is meaningfull for anything else ?
    """

    def save(self, bl_instance: Any, attr_name: str, context: Context):
        try:
            setattr(bl_instance, attr_name, None)
        except AttributeError as e:
            # Motsly errors like
            #   AttributeError: bpy_struct: attribute "node_tree" from "Material" is read-only
            # Avoiding them would require filtering attrivutes on save in order not to set
            # Material.node_tree if Material.use_nodes is False
            logger.debug("NonePtrProxy.save() exception ...")
            logger.debug(f"... {repr(e)}")

    def apply(
        self,
        parent: Any,
        key: str,
        delta: DeltaUpdate,
        context: Context,
        to_blender: bool = True,
    ) -> Optional[NonePtrProxy]:
        """
        Apply a delta to this proxy, which occurs when Scene.camera is set from None to a valid reference
        """
        update = delta.value

        if isinstance(update, DatablockRefProxy):
            if to_blender:
                datablock = context.proxy_state.datablocks.get(update._datablock_uuid)
                setattr(parent, key, datablock)
            return update

        # A none PointerProperty that can point to something that is not a datablock.
        # Can this happen ?
        logger.error(f"apply({parent}, {key}) called with a {type(update)} at {context.visit_state.path}")
        return None

    def diff(
        self,
        container: Union[T.bpy_prop_collection, T.Struct],
        key: Union[str, int],
        prop: T.Property,
        context: Context,
    ) -> Optional[DeltaUpdate]:
        attr = read_attribute(container, key, prop, context)
        if isinstance(attr, NonePtrProxy):
            return None
        return DeltaUpdate(attr)
