import os
import qdrant_client
import asyncio



# 【重点新增】：给那些在网络墙内的同学，设置国内的 HuggingFace 镜像站，保证光速下载本地模型
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from llama_index.core import VectorStoreIndex, SimpleDirectoryReader, StorageContext, Settings
from llama_index.vector_stores.qdrant import QdrantVectorStore
from llama_index.llms.openai_like import OpenAILike
from app.core.config import settings

# 【核心新增】：导入基础后处理器模板，我们要自己写一个！
from typing import List, Optional
from llama_index.core.postprocessor.types import BaseNodePostprocessor
from llama_index.core.schema import NodeWithScore, QueryBundle
from pydantic import Field, PrivateAttr

# 【核心修改】：导入本地 HuggingFace 向量模型组件
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ================= 1. 配置全局模型 =================

# 这个依然是智谱的大语言模型（用来总结说话，它本身是不收基础费的）
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"

Settings.llm = OpenAILike(
    api_key=settings.ZHIPU_API_KEY,
    api_base=ZHIPU_BASE_URL,
    model="glm-4-flash",
    is_chat_model=True
)

# 【核心替换】：文官用智谱，武将（向量化）用完全免费的本地大模型！
# BAAI/bge-small-zh-v1.5 是中文界公认的小而美的开源向量模型
Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-zh-v1.5")


class MyCustomBGEReranker(BaseNodePostprocessor):
    """
    咱们自己手写的交叉重排器（完美规避三方套壳包的依赖泥潭）
    """
    top_n: int = Field(default=2)
    model_name: str = Field(default="BAAI/bge-reranker-base")
    _model: object = PrivateAttr()

    def __init__(self, model_name: str = "BAAI/bge-reranker-base", top_n: int = 2):
        super().__init__(model_name=model_name, top_n=top_n)
        # 直接使用最底层的 sentence-transformers 引擎，非常稳健！
        from sentence_transformers import CrossEncoder
        self._model = CrossEncoder(model_name, max_length=512)

    def _postprocess_nodes(
            self, nodes: List[NodeWithScore], query_bundle: Optional[QueryBundle] = None
    ) -> List[NodeWithScore]:
        if not query_bundle or not nodes:
            return nodes

        # 【手工原理大揭秘】
        # 1. 组装材料：把用户的问句，和 Chroma 初筛出来的文本，一对一捆在一起
        sentence_pairs = [[query_bundle.query_str, node.node.get_content()] for node in nodes]

        # 2. 丢给大算力模型去算交叉得分（化学反应开始了）
        scores = self._model.predict(sentence_pairs)

        # 3. 把分数强行写回 LlamaIndex 的节点里
        for node, score in zip(nodes, scores):
            node.score = float(score)

        # 4. 排序！分数高的排在前面
        sorted_nodes = sorted(nodes, key=lambda x: x.score, reverse=True)
        # 5. 挥刀！只保留分数最高的 top_n 段给大模型
        return sorted_nodes[:self.top_n]


# ================= 1.5 挂载我们手造的重排器 =================
print("正在载入我们自己手搓的重排模型，请稍候...")
RERANKER_MODEL = MyCustomBGEReranker(model_name="BAAI/bge-reranker-base", top_n=2)

# 使用极其优雅的网络连接，连上本机的 Docker Qdrant 服务！
# 它是长连接，所以放在全局不会引发句柄爆满，Qdrant 原生处理了连接池
print("正在连接到远程独立 Qdrant 数据库...")
GLOBAL_QDRANT_CLIENT = qdrant_client.QdrantClient(host="localhost", port=6333)

# ================= 2. 知识库加载与查询逻辑（分离重构） =================

def _get_vector_store_and_context():
    """复用的辅助函数：找到 Qdrant 实例并连上去"""

    # 无需在本地用复杂的 os.path 乱找了，直接通过网络使用独立大数据库！
    # 建立一个叫 local_company_knowledge 的集合 (collection)
    vector_store = QdrantVectorStore(
        client=GLOBAL_QDRANT_CLIENT,
        collection_name="local_company_knowledge"
    )

    # 上传路径依然需要保留，因为在离线【冷任务】时，系统还是要找 txt 读取。
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    upload_dir = os.path.join(base_dir, "data", "uploads")

    return vector_store, upload_dir


def build_knowledge_base():
    """
    冷任务：只给管理员用的专属重建函数。
    读取全量文档，重新 Embedding 写入库中。
    """
    vector_store, upload_dir = _get_vector_store_and_context()
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    print("=== [开始离线构建任务] ===")
    documents = SimpleDirectoryReader(upload_dir).load_data()
    print(f"成功读取到 {len(documents)} 个文档片段，正在交由本地模型转化为数字存入库中...")

    # 发起致命重算！这里会耗时极长，但它只在后台跑，无所谓。
    index = VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context
    )
    print("=== [离线构建任务完成] ===")
    return True


def get_readonly_index():
    """
    热任务底层支持：拿出现成的库，不重新查硬盘读文件，光速返回索引！
    """
    vector_store, _ = _get_vector_store_and_context()

    # 核心改动：from_vector_store 替代了 from_documents，纯天然无污染读取
    index = VectorStoreIndex.from_vector_store(
        vector_store=vector_store
    )
    return index


async def query_knowledge(question: str) -> str:
    """热任务：用户在高低并发下调用的查询"""
    # 1. 闪电拿库 (毫秒级)
    index = get_readonly_index()
    # 2. 组装终极查询器
    # 这里我们修改了创建 query_engine 的参数：
    # similarity_top_k=10  => ChromaDB（粗排）别怕多，给我大撒网把第一眼有关的 10 个全捞出来！
    # node_postprocessors  => 把那 10 个生吃进去，让咱们挂载的交叉重排 RERANKER_MODEL 去卡脖子！
    query_engine = index.as_query_engine(
        similarity_top_k=10,
        node_postprocessors=[RERANKER_MODEL]
    )
    # 3. 开始执行流水线：10进 -> 重排计分 -> 取2 -> 给大模型组织人话
    response = await asyncio.to_thread(query_engine.query, question)
    return f"{str(response)}\n\n[来源：系统经过精准重排提取]"