"""
配置管理 - 使用 Pydantic Settings v2
支持两种模式：公司内部 LiteLLM / 阿里云 DashScope
"""
from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict
from typing import List, Literal
import os
from pathlib import Path


# 每次实例化 Settings() 时，自动读取 .env 文件和环境变量，把值填充到对应字段里。
# 你不需要手动调用任何"加载函数"，这是 BaseSettings.__init__() 内部帮你做的。
class Settings(BaseSettings):
    # Pydantic v2 中定义类级别配置的方式
    # 去哪读：.env 文件的路径
    # 怎么读：UTF-8 编码
    # 额外字段：忽略
    # 字段映射：允许 alias 和字段名混用
    model_config = ConfigDict(
        # 用绝对路径定位 .env，__file__ 是 config.py 的路径，.. 回到项目根目录
        env_file=os.path.join(os.path.dirname(__file__), "..", "..", ".env"),
        env_file_encoding="utf-8",
        # 忽略 .env 文件中定义的额外字段
        extra="ignore",
        # 允许同时用字段名和 alias 赋值，更灵活
        populate_by_name=True,
    )
    # ==================== LLM 配置（当前使用内部模型） ====================
    # Literal 来自 typing 模块，意思是字面量类型——这个字段的值只能是括号里列出的那几个字符串之一，不能是其他值。
    # LLM 提供商，可选值有 "litellm_internal" / "dashscope"
    # Field 来自 Pydantic，用于给模型字段添加元数据和校验规则。
    llm_provider: Literal["litellm_internal", "dashscope"] = Field(
        default="litellm_internal", alias="LLM_PROVIDER"
    )

    # 公司内部 LiteLLM
    litellm_base_url: str = Field(default="http://192.168.0.159:4000", alias="ANTHROPIC_BASE_URL")
    litellm_api_key: str = Field(default="", alias="ANTHROPIC_AUTH_TOKEN")
    litellm_model: str = Field(default="Qwen3-Coder-Next-FP8", alias="ANTHROPIC_MODEL")

    # ==================== 阿里云 DashScope 配置（备用） ====================
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_key: str = os.getenv("DASHSCOPE_API_KEY", "")
    dashscope_model: str = "qwen3.7-plus"

    # ==================== 数据源配置 ====================
    default_data_source: str = "sqlite_demo"
    sqlite_db_path: str = "./data/demo.db"

    # ==================== 用户模块 MySQL 配置 ====================
    # 优先使用完整 SQLAlchemy URL；为空时使用下方 MYSQL_* 配置拼接。
    # 示例：mysql+pymysql://root:password@127.0.0.1:3306/nl2sql_presearch?charset=utf8mb4
    user_database_url: str = Field(default="", alias="USER_DATABASE_URL")
    mysql_host: str = Field(default="127.0.0.1", alias="MYSQL_HOST")
    mysql_port: int = Field(default=3306, alias="MYSQL_PORT")
    mysql_user: str = Field(default="root", alias="MYSQL_USER")
    mysql_password: str = Field(default="", alias="MYSQL_PASSWORD")
    mysql_database: str = Field(default="nl2sql_presearch", alias="MYSQL_DATABASE")
    mysql_charset: str = Field(default="utf8mb4", alias="MYSQL_CHARSET")

    # ==================== 安全与健壮性 ====================
    """
    这三个配置是 NL2SQL 系统的防护措施：
    max_rows_return：NL2SQL 生成的 SQL 可能查出海量数据，限制行数防止资源耗尽。
    enable_self_correction + max_correction_attempts：LLM 生成的 SQL 不一定正确（语法错误、表名错误等），开启自纠错后，系统会把报错信息反馈给 LLM 让它重新生成，但最多重试 2 次，避免死循环
    """
    max_rows_return: int = 1000  # SQL 查询最多返回 1000 行，防止一次性拉取太多数据撑爆内存
    enable_self_correction: bool = True  # 开启自纠错：当生成的 SQL 执行报错时，让 LLM 自动修正
    max_correction_attempts: int = 2  # 最多尝试纠错 2 次，防止无限循环

    # ==================== 审计 ====================
    """
    用户提了什么自然语言问题
    系统生成了什么 SQL
    SQL 执行结果是什么
    是否触发了自纠错
    操作时间、耗时等
    """
    audit_db_path: str = "AUDIT_DB_PATH"

    # ==================== CORS ====================
    # 允许所有的源 域名列表
    cors_origins: List[str] = ["*"]

    """
    第一步：Config 类告诉 Pydantic 去哪读 
    第二步：alias 建立映射关系alias="LLM_PROVIDER" 告诉 Pydantic：去 .env 里找 LLM_PROVIDER 这个键
    第三步：BaseSettings 自动完成读取
    BaseSettings 和普通 Pydantic BaseModel 
    的区别就在于：它会在实例化时自动从环境变量 / .env 文件中加载值
    """
    # class Config 告诉程序 "去哪里读配置、怎么读"
    # class Config:
    #     env_file = ".env"
    #     env_file_encoding = "utf-8"
    #     extra = "ignore"
# ==================== 动态属性（方便其他模块调用） ====================
    # 调用时像普通属性一样，不加括号 属性的简洁写法，又保留了方法的灵活逻辑
    # @property 把方法变成属性
    @property
    def llm_base_url(self) -> str:
        if self.llm_provider == "litellm_internal":
            return self.litellm_base_url
        else:
            return self.dashscope_base_url
    @property
    def llm_api_key(self) -> str:
        if self.llm_provider == "litellm_internal":
            return self.litellm_api_key
        else:
            return self.dashscope_api_key
    @property
    def llm_model(self) -> str:
        if self.llm_provider == "litellm_internal":
            return self.litellm_model
        else:
            return self.dashscope_model
settings = Settings()

os.makedirs("../../../codex/work_extract/backend/nl2sql-presearch/backend/config/data", exist_ok=True)

if __name__ == "__main__":

    print(f"✅ 配置加载成功")
    print(f"   当前 Provider : {settings.llm_provider}")
    print(f"   当前模型      : {settings.llm_model}")
    print(f"   Base URL      : {settings.llm_base_url}")
    print(f"   API Key 已配置: {'是' if settings.llm_api_key else '否'}")
