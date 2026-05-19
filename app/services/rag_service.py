import os
import chromadb


# 【重点新增】：给那些在网络墙内的同学，设置国内的 HuggingFace 镜像站，保证光速下载本地模型
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from llama_index.core import VectorStoreIndex, SimpleDirectoryReader, StorageContext, Settings
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.llms.openai_like import OpenAILike
from app.core.config import settings

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

# 单例模式，全局数据库单例
base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
data_dir = os.path.join(base_dir, "data")
upload_dir = os.path.join(data_dir, "uploads")
db_dir = os.path.join(data_dir, "chroma_db")
GLOBAL_DB_CLIENT = chromadb.PersistentClient(path=db_dir)

# ================= 2. 知识库加载与查询逻辑（分离重构） =================

def _get_vector_store_and_context():
    """复用的辅助函数：找到 ChromaDB 实例并连上去"""

    chroma_collection = GLOBAL_DB_CLIENT.get_or_create_collection("local_company_knowledge")

    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)

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


def query_knowledge(question: str) -> str:
    """热任务：用户在高低并发下调用的查询"""
    # 1. 闪电拿库 (毫秒级)
    index = get_readonly_index()
    # 2. 查询
    query_engine = index.as_query_engine()
    response = query_engine.query(question)
    return f"{str(response)}\n\n[来源：系统私有知识库]"