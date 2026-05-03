import os
import chromadb


# 【重点新增】：给那些在网络墙内的同学，设置国内的 HuggingFace 镜像站，保证光速下载本地模型
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from llama_index.core import VectorStoreIndex, SimpleDirectoryReader, StorageContext, Settings
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.llms.openai_like import OpenAILike
from app.core.config import ZHIPU_API_KEY

# 【核心修改】：导入本地 HuggingFace 向量模型组件
from llama_index.embeddings.huggingface import HuggingFaceEmbedding

# ================= 1. 配置全局模型 =================

# 这个依然是智谱的大语言模型（用来总结说话，它本身是不收基础费的）
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"

Settings.llm = OpenAILike(
    api_key=ZHIPU_API_KEY,
    api_base=ZHIPU_BASE_URL,
    model="glm-4-flash",
    is_chat_model=True
)

# 【核心替换】：文官用智谱，武将（向量化）用完全免费的本地大模型！
# BAAI/bge-small-zh-v1.5 是中文界公认的小而美的开源向量模型
Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-zh-v1.5")


# ================= 2. 知识库加载与查询逻辑 =================

def build_or_load_index():
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    data_dir = os.path.join(base_dir, "data")
    upload_dir = os.path.join(data_dir, "uploads")
    db_dir = os.path.join(data_dir, "chroma_db")

    db_client = chromadb.PersistentClient(path=db_dir)

    # 因为换了新模型（数字维数变了），为了防止和刚刚调智谱失败残留的废库冲突，我们换个新表名！
    chroma_collection = db_client.get_or_create_collection("local_company_knowledge")

    vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    documents = SimpleDirectoryReader(upload_dir).load_data()
    print(f"成功读取到 {len(documents)} 个文档片段，正在交由本地模型转化为数字存入库中...")

    index = VectorStoreIndex.from_documents(
        documents,
        storage_context=storage_context
    )
    return index


def query_knowledge(question:str) -> str:
    index = build_or_load_index()
    query_engine = index.as_query_engine()
    response = query_engine.query(question)
    return f"{str(response)}\n\n[来源：系统私有知识库]"