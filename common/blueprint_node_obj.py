import bpy
from bpy.types import NodeTree, Node, NodeSocket

from .logic_name import LogicName
from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .blueprint_node_base import SSMTBlueprintTree, SSMTNodeBase

BLENDER_VERSION = bpy.app.version[:2]

_picking_node_name = None
_picking_tree_name = None
_is_viewing_group_objects = False

_msgbus_owner = object()

_selection_sync_in_progress = False
_last_selected_objects = frozenset()
_last_selected_nodes = frozenset()
_timer_interval = 0.5


def _get_all_selected_nodes():
    """
    获取所有蓝图节点树中被选中的节点。
    返回格式: frozenset of (tree_name, node_name) tuples
    """
    selected = set()
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'SSMTBlueprintTreeType':
            for node in tree.nodes:
                if node.select:
                    selected.add((tree.name, node.name))
    return frozenset(selected)


def _get_object_from_node(node):
    """
    从物体信息节点获取关联的物体。
    """
    if node.bl_idname == 'SSMTNode_Object_Info':
        obj_name = getattr(node, 'object_name', '')
        if obj_name:
            return bpy.data.objects.get(obj_name)
    return None


def _get_nodes_for_object(obj):
    """
    获取所有引用指定物体的物体信息节点。
    """
    nodes = []
    obj_name = obj.name
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'SSMTBlueprintTreeType':
            for node in tree.nodes:
                if node.bl_idname == 'SSMTNode_Object_Info':
                    if getattr(node, 'object_name', '') == obj_name:
                        nodes.append((tree, node))
    return nodes


def _sync_selection():
    """
    双向同步节点和物体的选择状态。
    通过比较当前选择状态与上次缓存的状态，决定同步方向。
    """
    global _selection_sync_in_progress, _last_selected_objects, _last_selected_nodes
    
    if _selection_sync_in_progress:
        return
    
    _selection_sync_in_progress = True
    try:
        current_objects = frozenset(obj.name for obj in bpy.context.selected_objects)
        current_nodes = _get_all_selected_nodes()
        
        objects_changed = current_objects != _last_selected_objects
        nodes_changed = current_nodes != _last_selected_nodes
        
        if not objects_changed and not nodes_changed:
            return
        
        if nodes_changed and not objects_changed:
            _last_selected_nodes = current_nodes
            target_objects = set()
            for tree in bpy.data.node_groups:
                if tree.bl_idname == 'SSMTBlueprintTreeType':
                    for node in tree.nodes:
                        if node.select:
                            obj = _get_object_from_node(node)
                            if obj:
                                target_objects.add(obj.name)
            
            for obj in bpy.data.objects:
                should_select = obj.name in target_objects
                if obj.select_get() != should_select:
                    obj.select_set(should_select)
            
            _last_selected_objects = frozenset(target_objects)
        
        elif objects_changed and not nodes_changed:
            _last_selected_objects = current_objects
            for tree in bpy.data.node_groups:
                if tree.bl_idname == 'SSMTBlueprintTreeType':
                    for node in tree.nodes:
                        if node.bl_idname == 'SSMTNode_Object_Info':
                            obj = _get_object_from_node(node)
                            should_select = obj and obj.name in current_objects
                            if node.select != should_select:
                                node.select = should_select
            
            _last_selected_nodes = _get_all_selected_nodes()
        
        else:
            _last_selected_nodes = current_nodes
            _last_selected_objects = current_objects
    finally:
        _selection_sync_in_progress = False


def _selection_timer_callback():
    """
    定时器回调函数，用于定期检查选择状态变化。
    主要用于检测节点选择变化（depsgraph 无法捕获节点选择变化）。
    """
    _sync_selection()
    return _timer_interval


def on_selection_update(scene):
    """
    选择状态更新回调函数。
    通过 depsgraph 更新处理器触发，检查并同步选择状态。
    主要用于捕获物体选择变化。
    """
    _sync_selection()


def _update_node_object_references():
    """
    更新所有物体信息节点中的物体引用。
    通过 object_id（物体内存指针）匹配物体，当物体名称发生变化时自动更新节点的 object_name。
    """
    for tree in bpy.data.node_groups:
        if tree.bl_idname == 'SSMTBlueprintTreeType':
            for node in tree.nodes:
                if node.bl_idname == 'SSMTNode_Object_Info':
                    obj_id = getattr(node, 'object_id', '')
                    if not obj_id:
                        continue
                    
                    for obj in bpy.data.objects:
                        if str(obj.as_pointer()) == obj_id:
                            if node.object_name != obj.name:
                                node.object_name = obj.name
                            break


def on_object_name_changed():
    """
    物体名称变更回调函数。
    当任何物体的 name 属性发生变化时，由 msgbus 触发此函数。
    """
    _update_node_object_references()


def on_undo_redo_post(scene):
    """
    撤销/重做操作完成后的回调函数。
    由于撤销/重做不会触发 msgbus 消息，需要通过 app handlers 来捕获这些操作。
    """
    _update_node_object_references()


def subscribe_to_object_name_changes():
    """
    订阅物体名称变更事件。
    1. 使用 msgbus 监听物体 name 属性的直接修改
    2. 使用 app handlers 监听撤销/重做操作
    """
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Object, "name"),
        owner=_msgbus_owner,
        args=(),
        notify=on_object_name_changed,
    )
    if on_undo_redo_post not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(on_undo_redo_post)
    if on_undo_redo_post not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(on_undo_redo_post)


def unsubscribe_from_object_name_changes():
    """
    取消订阅物体名称变更事件。
    清理 msgbus 订阅和 app handlers，防止内存泄漏。
    """
    bpy.msgbus.clear_by_owner(_msgbus_owner)
    if on_undo_redo_post in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(on_undo_redo_post)
    if on_undo_redo_post in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(on_undo_redo_post)


def subscribe_to_selection_sync():
    """
    订阅选择同步事件。
    1. 使用定时器定期检查节点选择变化
    2. 使用 depsgraph 更新处理器监听物体选择变化
    """
    if on_selection_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(on_selection_update)
    if not bpy.app.timers.is_registered(_selection_timer_callback):
        bpy.app.timers.register(_selection_timer_callback, persistent=True)


def unsubscribe_from_selection_sync():
    """
    取消订阅选择同步事件。
    清理定时器和 depsgraph 处理器，防止内存泄漏。
    """
    if on_selection_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(on_selection_update)
    if bpy.app.timers.is_registered(_selection_timer_callback):
        bpy.app.timers.unregister(_selection_timer_callback)



class SSMT_OT_RefreshNodeObjectIDs(bpy.types.Operator):
    '''刷新节点树中所有节点的物体ID关联'''
    bl_idname = "ssmt.refresh_node_object_ids"
    bl_label = "刷新物体ID关联"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        updated_count = 0
        
        for tree in bpy.data.node_groups:
            if tree.bl_idname == 'SSMTBlueprintTreeType':
                for node in tree.nodes:
                    if node.bl_idname == 'SSMTNode_Object_Info':
                        obj_name = getattr(node, 'object_name', '')
                        obj_id = getattr(node, 'object_id', '')
                        
                        if obj_name:
                            obj = bpy.data.objects.get(obj_name)
                            if obj:
                                new_obj_id = str(obj.as_pointer())
                                if node.object_id != new_obj_id:
                                    node.object_id = new_obj_id
                                    updated_count += 1
                            elif obj_id:
                                node.object_id = ""
                                updated_count += 1
        
        if updated_count > 0:
            self.report({'INFO'}, f"已更新 {updated_count} 个节点的物体引用")
           
        else:
            self.report({'INFO'}, "所有节点都已建立物体引用关联")
        
        return {'FINISHED'}


class SSMT_OT_SelectNodeObject(bpy.types.Operator):
    '''Select this object in 3D View'''
    bl_idname = "ssmt.select_node_object"
    bl_label = "Select Object"
    
    object_name: bpy.props.StringProperty() # type: ignore

    def execute(self, context):
        obj_name = self.object_name
        if not obj_name:
            return {'CANCELLED'}
        
        obj = bpy.data.objects.get(obj_name)
        if obj:
            try:
                bpy.ops.object.select_all(action='DESELECT')
            except:
                pass
                
            obj.select_set(True)
            context.view_layer.objects.active = obj
            self.report({'INFO'}, f"Selected: {obj_name}")
        else:
            self.report({'WARNING'}, f"Object '{obj_name}' not found")
        
        return {'FINISHED'}


class SSMT_OT_StartPickObject(bpy.types.Operator):
    '''Start picking an object from 3D View'''
    bl_idname = "ssmt.start_pick_object"
    bl_label = "Pick Object"
    bl_description = "点击后在3D视图中选择一个物体"
    
    node_name: bpy.props.StringProperty() # type: ignore
    
    def execute(self, context):
        global _picking_node_name, _picking_tree_name
        
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        
        if not tree:
            self.report({'WARNING'}, "无法获取节点树上下文")
            return {'CANCELLED'}
        
        _picking_node_name = self.node_name
        _picking_tree_name = tree.name
        self.report({'INFO'}, "请在3D视图中点击选择一个物体")
        
        bpy.ops.ssmt.pick_object_modal('INVOKE_DEFAULT')
        
        return {'FINISHED'}


class SSMT_OT_PickObjectModal(bpy.types.Operator):
    '''Modal operator for picking objects in 3D View'''
    bl_idname = "ssmt.pick_object_modal"
    bl_label = "Pick Object"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    def invoke(self, context, event):
        global _picking_node_name
        
        if not _picking_node_name:
            return {'CANCELLED'}
        
        self._initial_selected_objs = set(context.selected_objects)
        if context.selected_objects:
            self._last_selected_obj = context.selected_objects[0]
        else:
            self._last_selected_obj = None
        
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        global _picking_node_name, _picking_tree_name
        
        if event.type == 'ESC':
            _picking_node_name = None
            _picking_tree_name = None
            return {'CANCELLED'}
        
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                    if region and area.x <= event.mouse_x <= area.x + area.width and area.y <= event.mouse_y <= area.y + area.height:
                        return {'PASS_THROUGH'}
        
        if event.type == 'MOUSEMOVE':
            current_selected = context.selected_objects
            if current_selected:
                current_obj = current_selected[0]
                if current_obj != self._last_selected_obj and current_obj not in self._initial_selected_objs:
                    tree = bpy.data.node_groups.get(_picking_tree_name)
                    if tree:
                        node = tree.nodes.get(_picking_node_name)
                        if node:
                            node.object_name = current_obj.name
                            self.report({'INFO'}, f"已选择物体: {current_obj.name}")
                    
                    _picking_node_name = None
                    _picking_tree_name = None
                    return {'FINISHED'}
        
        return {'PASS_THROUGH'}


def draw_view3d_header(self, context):
    global _picking_node_name
    if _picking_node_name:
        self.layout.label(text="请在3D视图中点击选择一个物体...", icon='EYEDROPPER')


class SSMTNode_Object_Info(SSMTNodeBase):
    '''Object Info Node'''
    bl_idname = 'SSMTNode_Object_Info'
    bl_label = 'Object Info'
    bl_icon = 'OBJECT_DATAMODE'
    bl_width_min = 300
    
    def update_object_name(self, context):
        if self.object_name:
            self.label = self.object_name
            if "-" in self.object_name:
                obj_name_total_split = self.object_name.split(".")

                obj_name_split = obj_name_total_split[0].split("-")

                self.draw_ib = obj_name_split[0]
                self.index_count = obj_name_split[1]
                self.first_index = obj_name_split[2]

                # alias_name should be everything after the first '.'
                if len(obj_name_total_split) >= 2:
                    self.alias_name = ".".join(obj_name_total_split[1:])
                else:
                    self.alias_name = ""
            
            obj = bpy.data.objects.get(self.object_name)
            if obj:
                self.object_id = str(obj.as_pointer())
        else:
            self.label = "Object Info"
            self.object_id = ""
        
        self.update_node_width([self.object_name, self.draw_ib, self.index_count, self.first_index, self.alias_name])
    object_name: bpy.props.StringProperty(name="Object Name", default="", update=update_object_name) #type: ignore
    object_id: bpy.props.StringProperty(name="Object ID", default="") #type: ignore
    original_object_name: bpy.props.StringProperty(name="Original Object Name", default="") #type: ignore


    draw_ib: bpy.props.StringProperty(name="DrawIB", default="") # type: ignore
    index_count: bpy.props.StringProperty(name="IndexCount", default="") # type: ignore
    first_index: bpy.props.StringProperty(name="FirstIndex", default="") # type: ignore
    alias_name: bpy.props.StringProperty(name="Alias Name", default="") # type: ignore

    def init(self, context):
        self.outputs.new('SSMTSocketObject', "Object")

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)

        row.prop_search(self, "object_name", bpy.data, "objects", text="", icon='OBJECT_DATA')
        
        op = row.operator("ssmt.start_pick_object", text="", icon='EYEDROPPER')
        op.node_name = self.name

        if self.object_name:
            op = row.operator("ssmt.select_node_object", text="", icon='RESTRICT_SELECT_OFF')
            op.object_name = self.object_name

        # Display as read-only labels to prevent user edits in the UI
        layout.label(text=f"DrawIB: {self.draw_ib}")
        layout.label(text=f"IndexCount: {self.index_count}")
        layout.label(text=f"FirstIndex: {self.first_index}")
        layout.label(text=f"Alias Name: {self.alias_name}")


class SSMTNode_Object_Group(SSMTNodeBase):
    '''单纯用于分组的节点，可以接受任何节点作为输入，放在一个组里'''
    bl_idname = 'SSMTNode_Object_Group'
    bl_label = 'Group'
    bl_icon = 'GROUP'

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Input 1")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 200

    def draw_buttons(self, context, layout):
        layout.operator("ssmt.view_group_objects", text="查看递归解析预览", icon='HIDE_OFF').node_name = self.name

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"Input {len(self.inputs) + 1}")
        
        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
             self.inputs.remove(self.inputs[-1])




class SSMT_OT_SwitchKey_AddSocket(bpy.types.Operator):
    '''Add a new socket to the switch node'''
    bl_idname = "ssmt.switch_add_socket"
    bl_label = "Add Socket"
    
    node_name: bpy.props.StringProperty() # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
             return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node:
             node.inputs.new('SSMTSocketObject', f"Status {len(node.inputs)}")
        return {'FINISHED'}


class SSMT_OT_SwitchKey_RemoveSocket(bpy.types.Operator):
    '''Remove the last socket from the switch node'''
    bl_idname = "ssmt.switch_remove_socket"
    bl_label = "Remove Socket"
    
    node_name: bpy.props.StringProperty() # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
             return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node and len(node.inputs) > 0:
            node.inputs.remove(node.inputs[-1])
        return {'FINISHED'}


class SSMTNode_SwitchKey(SSMTNodeBase):
    '''【按键切换】会把每个连入的分支分配到单独的变量'''
    bl_idname = 'SSMTNode_SwitchKey'
    bl_label = 'Switch Key'
    bl_icon = 'GROUP'

    def update_key_name(self, context):
        self.update_node_width([self.key_name, self.comment])
    
    def update_comment(self, context):
        self.update_node_width([self.key_name, self.comment])
    
    key_name: bpy.props.StringProperty(name="Key Name", default="", update=update_key_name) # type: ignore
    comment: bpy.props.StringProperty(name="备注", description="备注信息，会以注释形式生成到配置表中", default="", update=update_comment) # type: ignore
    
    def init(self, context):
        self.label = "按键切换"
        self.inputs.new('SSMTSocketObject', "Status 0")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 200
        self.use_custom_color = True
        self.color = (0.34, 0.54, 0.34)

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        row.prop(self, "key_name", text="按键")
        row.operator("wm.url_open", text="", icon='HELP').url = "https://learn.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes"
        
        layout.prop(self, "comment", text="备注")
        
        row = layout.row(align=True)
        op_add = row.operator("ssmt.switch_add_socket", text="Add", icon='ADD')
        op_add.node_name = self.name
        
        op_rem = row.operator("ssmt.switch_remove_socket", text="Remove", icon='REMOVE')
        op_rem.node_name = self.name


class SSMTNode_Result_Output(SSMTNodeBase):
    '''Result Output Node'''
    bl_idname = 'SSMTNode_Result_Output'
    bl_label = 'Generate Mod'
    bl_icon = 'EXPORT'

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Group 1")
        self.outputs.new('SSMTSocketPostProcess', "Post Process")
        self.width = 400

    def draw_buttons(self, context, layout):
        layout.operator("ssmt.generate_mod_blueprint", text="Generate Mod", icon='EXPORT')
        
        if GlobalConfig.logic_name == LogicName.WWMI:
            layout.prop(context.scene.global_properties, "ignore_muted_shape_keys")
            layout.prop(context.scene.global_properties, "apply_all_modifiers")
            layout.prop(context.scene.global_properties, "export_add_missing_vertex_groups")

        layout.prop(context.scene.global_properties, 
                    "forbid_auto_texture_ini",text="禁止自动贴图流程")

        if GlobalConfig.logic_name != LogicName.GF2:
            layout.prop(context.scene.global_properties,
                        "recalculate_tangent",text="向量归一化法线存入TANGENT(全局)")

        if GlobalConfig.logic_name == LogicName.HIMI:
            layout.prop(context.scene.global_properties,
                        "recalculate_color",text="算术平均归一化法线存入COLOR(全局)")

        if GlobalConfig.logic_name == LogicName.ZZMI:
            layout.prop(context.scene.global_properties, "zzz_use_slot_fix")

        if GlobalConfig.logic_name == LogicName.GIMI:
            layout.prop(context.scene.global_properties, "gimi_use_orfix")

        layout.prop(context.scene.global_properties, "generate_branch_mod_gui",text="生成分支架构Mod面板(测试中)")

        layout.prop(context.scene.global_properties, "open_mod_folder_after_generate_mod",text="生成Mod后打开Mod所在文件夹")

        layout.prop(context.scene.global_properties, "use_specific_generate_mod_folder_path")

        if GlobalProterties.use_specific_generate_mod_folder_path():
            box = layout.box()
            box.label(text="当前生成Mod位置文件夹:")
            box.label(text=context.scene.global_properties.generate_mod_folder_path)

            layout.operator("ssmt.select_generate_mod_folder", icon='FILE_FOLDER')

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"Group {len(self.inputs) + 1}")
        
        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
             self.inputs.remove(self.inputs[-1])


class SSMT_OT_View_Group_Objects(bpy.types.Operator):
    '''递归解析当前组下面所有的物体并在当前3D视图中展示，点击切换局部视图，注意组节点最好不要包含按键切换，否则会同时展示所有切换分支内容'''
    bl_idname = "ssmt.view_group_objects"
    bl_label = "View Group Objects"
    
    node_name: bpy.props.StringProperty() # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
             return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node:
             return {'CANCELLED'}

        view_3d_area = None
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    view_3d_area = area
                    break
            if view_3d_area:
                break
        
        if not view_3d_area:
            self.report({'WARNING'}, "No 3D View found")
            return {'CANCELLED'}

        in_local_view = False
        for space in view_3d_area.spaces:
            if space.type == 'VIEW_3D' and space.local_view:
                in_local_view = True
                break
        
        if in_local_view:
            with context.temp_override(area=view_3d_area):
                bpy.ops.view3d.localview()
            self.report({'INFO'}, "Exited local view")
            return {'FINISHED'}

        objects_to_show = set()
        checked_nodes = set()
        visited_blueprints = set()

        def collect_objects(current_node):
            if current_node in checked_nodes: 
                return
            checked_nodes.add(current_node)

            if getattr(current_node, "bl_idname", "") == 'SSMTNode_Object_Info':
                obj_name = getattr(current_node, "object_name", "")
                if obj_name:
                    obj = bpy.data.objects.get(obj_name)
                    if obj:
                        objects_to_show.add(obj)


            if hasattr(current_node, "inputs"):
                for inp in current_node.inputs:
                    if inp.is_linked:
                        for link in inp.links:
                            collect_objects(link.from_node)

        collect_objects(node)
        
        if not objects_to_show:
            self.report({'WARNING'}, "No objects found in this group")
            return {'CANCELLED'}

        def deselect_all_safe():
            for o in bpy.context.selected_objects:
                o.select_set(False)

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        deselect_all_safe()
        for obj in objects_to_show:
            obj.select_set(True)

        region = next((r for r in view_3d_area.regions if r.type == 'WINDOW'), None)
        if region:
            with context.temp_override(area=view_3d_area, region=region):
                try:
                    bpy.ops.view3d.localview()
                    bpy.ops.view3d.view_axis(type='FRONT')
                    bpy.ops.view3d.view_selected()
                    if view_3d_area.spaces.active:
                        view_3d_area.spaces.active.shading.type = 'SOLID'
                except Exception as e:
                    print(f"View setup warning: {e}")

        self.report({'INFO'}, f"Showing {len(objects_to_show)} objects in local view")
        return {'FINISHED'}


classes = (
    SSMT_OT_RefreshNodeObjectIDs,
    SSMT_OT_SelectNodeObject,
    SSMT_OT_StartPickObject,
    SSMT_OT_PickObjectModal,
    SSMT_OT_View_Group_Objects,
    SSMTNode_Object_Info,
    SSMTNode_Object_Group,
    SSMTNode_Result_Output,
    SSMTNode_SwitchKey,
    SSMT_OT_SwitchKey_AddSocket,
    SSMT_OT_SwitchKey_RemoveSocket,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_HT_header.append(draw_view3d_header)
    subscribe_to_object_name_changes()
    subscribe_to_selection_sync()


def unregister():
    unsubscribe_from_selection_sync()
    unsubscribe_from_object_name_changes()
    bpy.types.VIEW3D_HT_header.remove(draw_view3d_header)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
