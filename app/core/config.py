import os
from dotenv import load_dotenv

# 让程序启动时，去寻找并撬开那个叫做 .env 的保险箱
load_dotenv()

# 从保险箱里把指定的钥匙拿出来，放到内存变量里
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")

# 加一个安全保险，如果在 .env 里没找到钥匙，程序直接报错罢工，防止带着错的数据跑跑半天不知原因
if not ZHIPU_API_KEY:
    raise ValueError("找不到智谱 API_KEY，请检查根目录下的 .env 文件是否配置正确！")