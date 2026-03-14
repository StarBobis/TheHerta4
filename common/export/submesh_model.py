from dataclasses import dataclass, field
from typing import Dict
from .draw_call_model import DrawCallModel
from ...base.utils.obj_utils import ObjUtils
from ...helper.obj_buffer_helper import ObjBufferHelper

import bpy
'''
一般DrawIB索引缓冲区是由多个SubMesh子网格构成的
每个Submesh分别具有不同的材质和内容
所以这里沿用术语Submesh

因为我们可以通过DrawIndexed多次来绘制一个Submesh
所以Submesh是由多个Blender中的obj组成的

也就是在初始化的时候，遍历BlueprintModel中所有的obj
按照first_index,index_count,draw_ib来组在一起变成一个个Submesh
每个Submesh都包含1到多个obj
最后BluePrintModel可以得到一个SubmeshModel列表

然后就是数据的组合和数据的导出了
IB、CategoryBuffer要先组合在一起

然后在SubmeshModel之上，部分游戏还需要进行DrawIB级别的组合。
EFMI这个游戏只需要SubmeshModel级别的组合就行了，然后直接生成Mod
但是像GIMI这种游戏还需要在SubmeshModel之上进行DrawIB级别的组合，最后生成Mod

所以基于这个架构才是比较清晰的，SubmeshModel只负责Submesh级别的组合和数据导出
DrawIBModel负责DrawIB级别的组合和数据导出

TODO 
这里还有个问题，那就是在Blender中先组合出临时obj，再计算IB，VB，还是先计算IB，VB，再组合数据
这是个问题。

'''
@dataclass
class SubMeshModel:
    # 初始化时需要填入此属性
    drawcall_model_list:list[DrawCallModel] = field(default_factory=list)

    # post_init中计算得到这些属性
    match_draw_ib:str = field(init=False, default="")
    match_first_index:int = field(init=False, default=-1)
    match_index_count:int = field(init=False, default=-1)

    # 调用组合obj并计算ib和vb得到这些属性
    vertex_count:int = field(init=False, default=0)
    index_count:int = field(init=False, default=0)

    ib:list = field(init=False,repr=False,default_factory=list)
    category_buffer_dict:dict = field(init=False,repr=False,default_factory=dict)
    index_vertex_id_dict:dict = field(init=False,repr=False,default_factory=dict) 

    def __post_init__(self):

        # 因为列表里的每个DrawCallModel的draw_ib,first_index,index_count都是一样的，所以直接取第一个就行了
        if len(self.drawcall_model_list) > 0:
            self.match_draw_ib = self.drawcall_model_list[0].match_draw_ib
            self.match_first_index = self.drawcall_model_list[0].match_first_index
            self.match_index_count = self.drawcall_model_list[0].match_index_count
    

    def calc_buffer(self):
        # 对每个obj都创建一个临时对象进行处理，这样不影响原本的对象

        index_offset = 0
        submesh_temp_obj_list = []
        for draw_call_model in self.drawcall_model_list:
            # 获取到原本的obj
            source_obj = ObjUtils.get_obj_by_name(draw_call_model.obj_name)

            # 创建一个新的obj
            temp_obj = ObjUtils.copy_object(
                context=bpy.context,
                source_obj=source_obj,
                name=source_obj.name + "_temp",
                collection=bpy.context.collection
            )


            # 三角化obj
            ObjUtils.triangulate_object(bpy.context, temp_obj)

            # 计算其额外属性，因为for里拿到的是引用，所以原地修改即可
            draw_call_model.vertex_count = len(temp_obj.data.vertices)
            # 因为三角化了，所以每个面都是3个索引，所以 *3 就没问题
            # 所以上面那一步三角化是必须的，否则概率报错
            draw_call_model.index_count = len(temp_obj.data.polygons) * 3
            draw_call_model.index_offset = index_offset

            index_offset += draw_call_model.index_count

            # 这里赋值的意义在于，后续可能会合并到DrawIB级别
            # 这里就可以直接复用了
            self.vertex_count += draw_call_model.vertex_count
            self.index_count += draw_call_model.index_count

            # 临时对象放到列表里，后续进行合并
            submesh_temp_obj_list.append(temp_obj)

        # 接下来合并obj，合并的意义在于可以减少IB和VB的计算次数，在大批量导出时节省很多时间
        # 确保选中第一个，否则join_objects会报错
        if submesh_temp_obj_list:
            # 取消选中所有物体
            bpy.ops.object.select_all(action='DESELECT')

            # 选中第一个物体并设置为活动物体
            target_active = submesh_temp_obj_list[0]
            target_active.select_set(True)
            bpy.context.view_layer.objects.active = target_active

        # 执行物体合并
        ObjUtils.join_objects(bpy.context, submesh_temp_obj_list)
        
        # 因为合并到第一个obj上了，所以这里直接拿到这个obj
        submesh_merged_obj = submesh_temp_obj_list[0]

        # 重命名为指定名称，等待后续操作
        merged_obj_name = "TEMP_SUBMESH_MERGED_" + draw_call_model.get_unique_str()
        ObjUtils.rename_object(submesh_merged_obj, merged_obj_name)

        # 这里我们需要拿到当前这个Obj的数据类型
        # 此时需要去读取工作空间中对应import.json中的d3d11_element_list
        # 此时定位到工作空间的文件夹，需要用unique_str
        folder_name = draw_call_model.get_unique_str()

        # 还需要定位到具体导入时导入的是哪个数据类型
        # 这个在一键导入的时候记录到当前工作空间下的Import.json中了
        
        

        # 检查并校验是否有缺少的元素
        # ObjBufferHelper.check_and_verify_attributes(obj=submesh_merged_obj, d3d11_game_type=self.d3d11GameType)
        


        
        # 1.合并当前obj列表的obj到一个临时obj

        
        # 2.对临时obj进行预处理
        
        # 3.计算临时obj的ib,category_buffer_dict,index_vertex_id_dict等数据，赋值给类属性

        # 4.计算完成后，删除临时obj
        pass