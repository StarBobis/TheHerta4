"""Blueprint node that exports a GIMI face morph mod from SSMT workspace data."""
from __future__ import annotations

import os

import bpy
import numpy
from bpy_extras.io_utils import ImportHelper

from ..common.global_config import GlobalConfig, LogicName
from ..utils.format_utils import Fatal
from ..utils.gimi_face_mod import FACE_VERTEX_STRIDE, FaceModPart, build_key_bytes, gimi_face_local_to_game_positions, slice_face_base_buffer, write_face_mod
from ..workspace.ssmt_workspace import SSMTWorkSpace
from ..workspace.submesh_json import SubmeshJson
from .blueprint_export_helper import BlueprintExportHelper
from .blueprint_node_base import SSMTNodeBase
from .blueprint_node_obj import ObjectPersistentIdManager


class FaceModExportError(ValueError):
    pass


def _collect_object_nodes(node, visited=None):
    """Resolve Object Info nodes upstream of a face-export node in socket order."""
    visited = visited if visited is not None else set()
    if node is None or node in visited:
        return []
    visited.add(node)

    if getattr(node, "bl_idname", "") == "SSMTNode_Object_Info":
        return [node]

    result = []
    for source_node in BlueprintExportHelper.get_connected_nodes(node):
        result.extend(_collect_object_nodes(source_node, visited))
    return result


def _get_face_position_buffer(submesh_json: SubmeshJson):
    for category_buffer in submesh_json.CategoryBufferList:
        if category_buffer.Type != "Normal" or category_buffer.Stride != FACE_VERTEX_STRIDE:
            continue
        elements = category_buffer.D3D11ElementList
        semantics = [element.SemanticName for element in elements]
        formats = [element.Format for element in elements]
        if semantics == ["POSITION", "NORMAL", "TANGENT"] and formats == [
            "R32G32B32_FLOAT",
            "R32G32B32_FLOAT",
            "R32G32B32A32_FLOAT",
        ]:
            return category_buffer
    raise FaceModExportError(
        "提取数据不是 GIMI 面部所需的 40 字节 POSITION/NORMAL/TANGENT vb0 布局。"
    )


def _read_mesh_positions(obj) -> numpy.ndarray:
    if getattr(obj, "type", "") != "MESH":
        raise FaceModExportError(f"物体 '{obj.name}' 不是网格物体。")
    if getattr(obj, "mode", "") == "EDIT":
        obj.update_from_editmode()

    # Face mods intentionally ignore object-level transforms.  Only mesh-local
    # edits participate in the position delta written to key.buf.
    positions = numpy.empty(len(obj.data.vertices) * 3, dtype=numpy.float32)
    obj.data.vertices.foreach_get("co", positions)
    return gimi_face_local_to_game_positions(positions.reshape(-1, 3))


def _build_face_part(object_node) -> FaceModPart:
    obj = ObjectPersistentIdManager.resolve_node_target(object_node, allow_name_fallback=True)
    if obj is None:
        raise FaceModExportError(f"物体节点 '{object_node.name}' 没有可用的 Blender 物体。")

    # SSMT4 will eventually provide face-model classification metadata.  Use
    # it here to validate or automatically select compatible face submeshes.
    submesh_name = object_node._get_effective_parse_name()
    if not submesh_name:
        raise FaceModExportError(f"物体 '{obj.name}' 没有关联 Submesh。")

    source_path = SSMTWorkSpace.check_and_get_submesh_json_path(submesh_name)
    submesh_json = SubmeshJson(source_path)
    position_buffer = _get_face_position_buffer(submesh_json)
    vertex_hash = str(submesh_json.CategoryHash.get("Position", "") or "").strip()
    if not vertex_hash:
        raise FaceModExportError(f"Submesh '{submesh_name}' 缺少 Position category hash。")
    if not os.path.isfile(position_buffer.FilePath):
        raise FaceModExportError(f"找不到原始 Position 缓冲: {position_buffer.FilePath}")

    base_bytes = slice_face_base_buffer(
        position_buffer.FilePath,
        vertex_offset=submesh_json.VertexOffset,
        vertex_count=submesh_json.VertexCount,
    )
    key_bytes = build_key_bytes(_read_mesh_positions(obj))
    if len(base_bytes) != len(key_bytes):
        raise FaceModExportError(
            f"物体 '{obj.name}' 顶点数已改变（原始 {len(base_bytes) // FACE_VERTEX_STRIDE}，"
            f"当前 {len(key_bytes) // FACE_VERTEX_STRIDE}）。面部 Mod 只能导出不改变拓扑的编辑。"
        )

    return FaceModPart(
        name=str(getattr(object_node, "submesh_name", "") or obj.name),
        vertex_hash=vertex_hash,
        base_bytes=base_bytes,
        key_bytes=key_bytes,
    )


def export_face_mod_from_node(node) -> tuple[str, int]:
    if GlobalConfig.logic_name != LogicName.GIMI:
        raise FaceModExportError("导出面部 Mod 仅支持 GIMI / 原神工作空间。")

    object_nodes = _collect_object_nodes(node)
    if not object_nodes:
        raise FaceModExportError("请将至少一个“物体信息”节点连接到“导出面部 Mod”节点。")

    parts = []
    seen_submeshes = set()
    for object_node in object_nodes:
        submesh_name = object_node._get_effective_parse_name()
        if submesh_name in seen_submeshes:
            continue
        seen_submeshes.add(submesh_name)
        parts.append(_build_face_part(object_node))

    output_folder = str(getattr(node, "output_folder", "") or "").strip()
    if not output_folder:
        output_folder = os.path.join(GlobalConfig.path_generate_mod_folder(), "FaceMod")
    result_path = write_face_mod(output_folder, parts, diffuse_hash=node.diffuse_hash)
    return result_path, len(parts)


class SSMTNode_Face_Mod_Export(SSMTNodeBase):
    bl_idname = "SSMTNode_Face_Mod_Export"
    bl_label = "导出面部 Mod"
    bl_icon = "MOD_MASK"

    output_folder: bpy.props.StringProperty(
        name="输出文件夹",
        description="留空时输出到常规 Mod 目录下的 FaceMod 文件夹",
        default="",
        subtype="DIR_PATH",
    )  # type: ignore
    diffuse_hash: bpy.props.StringProperty(
        name="Diffuse Hash",
        description="可选。仅作为角色范围限制，不会修改 diffuse 贴图",
        default="",
    )  # type: ignore
    open_folder: bpy.props.BoolProperty(name="导出后打开文件夹", default=True)  # type: ignore

    def init(self, context):
        self.outputs.new("SSMTSocketObject", "输出")
        self.inputs.new("SSMTSocketObject", "面部组 1")
        self.width = 360
        self.use_custom_color = True
        self.color = (0.58, 0.32, 0.12)

    def update(self):
        # Existing blend files predate the output socket.  Add it lazily when
        # Blender updates the node so old blueprints become chainable too.
        if len(self.outputs) == 0:
            self.outputs.new("SSMTSocketObject", "输出")
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new("SSMTSocketObject", f"面部组 {len(self.inputs) + 1}")
        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
            self.inputs.remove(self.inputs[-1])

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        operator = row.operator("ssmt.export_face_mod", text="导出面部 Mod", icon="EXPORT")
        operator.node_name = self.name
        operator.tree_name = self.id_data.name if self.id_data else ""

        layout.prop(self, "diffuse_hash", text="Diffuse Hash")
        folder_row = layout.row(align=True)
        folder_row.prop(self, "output_folder", text="输出")
        folder_operator = folder_row.operator("ssmt.select_face_mod_export_folder", text="", icon="FILE_FOLDER")
        folder_operator.node_name = self.name
        folder_operator.tree_name = self.id_data.name if self.id_data else ""
        layout.prop(self, "open_folder")


class SSMT_OT_ExportFaceMod(bpy.types.Operator):
    bl_idname = "ssmt.export_face_mod"
    bl_label = "导出面部 Mod"
    bl_description = "从 SSMT 工作空间的 GIMI 面部 vb0 生成 position delta 面部 Mod"
    bl_options = {"REGISTER"}

    node_name: bpy.props.StringProperty()  # type: ignore
    tree_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name) if self.tree_name else BlueprintExportHelper.get_current_blueprint_tree(context=context)
        node = tree.nodes.get(self.node_name) if tree and self.node_name else None
        if node is None or getattr(node, "bl_idname", "") != SSMTNode_Face_Mod_Export.bl_idname:
            self.report({"ERROR"}, "未找到面部 Mod 导出节点。")
            return {"CANCELLED"}

        try:
            output_folder, part_count = export_face_mod_from_node(node)
        except (FaceModExportError, Fatal, OSError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        self.report({"INFO"}, f"已导出 {part_count} 个面部部件: {output_folder}")
        if node.open_folder:
            try:
                os.startfile(output_folder)
            except OSError:
                pass
        return {"FINISHED"}


class SSMT_OT_SelectFaceModExportFolder(bpy.types.Operator, ImportHelper):
    bl_idname = "ssmt.select_face_mod_export_folder"
    bl_label = "选择面部 Mod 输出文件夹"
    bl_options = {"INTERNAL"}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")  # type: ignore
    node_name: bpy.props.StringProperty()  # type: ignore
    tree_name: bpy.props.StringProperty()  # type: ignore

    def invoke(self, context, event):
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        if node and node.output_folder:
            self.directory = node.output_folder
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.tree_name)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None:
            return {"CANCELLED"}
        node.output_folder = self.directory
        return {"FINISHED"}


_CLASSES = (
    SSMTNode_Face_Mod_Export,
    SSMT_OT_ExportFaceMod,
    SSMT_OT_SelectFaceModExportFolder,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
