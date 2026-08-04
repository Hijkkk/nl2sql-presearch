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
    llm_timeout: float = Field(default=60.0, alias="LLM_TIMEOUT")
    sql_generation_cache_seconds: int = Field(default=600, alias="SQL_GENERATION_CACHE_SECONDS")
    nl2sql_debug_output: bool = Field(default=True, alias="NL2SQL_DEBUG_OUTPUT")

    # ==================== 阿里云 DashScope 配置（备用） ====================
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    dashscope_model: str = Field(default="qwen3.7-max", alias="DASHSCOPE_MODEL")

    # 结果总结与 SQL 生成是两个不同任务：前者应使用低延迟、非思考的通用模型，
    # 不再占用 XiYanSQL 这个专用 Text-to-SQL 模型。
    result_summary_dashscope_enabled: bool = Field(default=True, alias="RESULT_SUMMARY_DASHSCOPE_ENABLED")
    result_summary_model: str = Field(
        default="qwen3.7-flash",
        alias="RESULT_SUMMARY_MODEL",
    )
    result_summary_max_tokens: int = Field(default=96, alias="RESULT_SUMMARY_MAX_TOKENS")
    result_summary_enable_thinking: bool = Field(default=False, alias="RESULT_SUMMARY_ENABLE_THINKING")


    # ==================== 本地 XiYanSQL 3B 配置（≤7B 课题约束） ====================
    xiyan_sql_3b_enabled: bool = Field(default=True, alias="XIYAN_SQL_3B_ENABLED")
    xiyan_sql_3b_base_url: str = Field(default="http://127.0.0.1:8010/v1", alias="XIYAN_SQL_3B_BASE_URL")
    xiyan_sql_3b_api_key: str = Field(default="", alias="XIYAN_SQL_3B_API_KEY")
    xiyan_sql_3b_model: str = Field(default="XiYanSQL-QwenCoder-3B-2504", alias="XIYAN_SQL_3B_MODEL")
    xiyan_sql_3b_model_path: str = Field(
        default=r"Z:\python\Projects\task\datasources\XiYanSQL-QwenCoder-3b\XiYanSQL-QwenCoder-3B-2504",
        alias="XIYAN_SQL_3B_MODEL_PATH",
    )

    # ==================== 本地 XiYanSQL 3B：Ollama 量化版 ====================
    xiyan_ollama_enabled: bool = Field(default=True, alias="XIYAN_OLLAMA_ENABLED")
    xiyan_ollama_base_url: str = Field(default="http://127.0.0.1:11434/v1", alias="XIYAN_OLLAMA_BASE_URL")
    xiyan_ollama_api_key: str = Field(default="ollama", alias="XIYAN_OLLAMA_API_KEY")
    xiyan_ollama_model: str = Field(default="xiyansql-3b", alias="XIYAN_OLLAMA_MODEL")

    # ==================== 本地 XiYanSQL 3B：训练/微调服务版 ====================
    xiyan_finetune_enabled: bool = Field(default=True, alias="XIYAN_FINETUNE_ENABLED")
    xiyan_finetune_base_url: str = Field(default="http://127.0.0.1:8010/v1", alias="XIYAN_FINETUNE_BASE_URL")
    xiyan_finetune_api_key: str = Field(default="", alias="XIYAN_FINETUNE_API_KEY")
    xiyan_finetune_model: str = Field(default="XiYanSQL-QwenCoder-3B-2504", alias="XIYAN_FINETUNE_MODEL")
    xiyan_finetune_model_path: str = Field(
        default=r"Z:\python\Projects\task\datasources\XiYanSQL-QwenCoder-3b\XiYanSQL-QwenCoder-3B-2504",
        alias="XIYAN_FINETUNE_MODEL_PATH",
    )

    # ==================== 数据源配置 ====================
    default_data_source: str = "sqlite_demo"
    sqlite_db_path: str = "./data/demo.db"

    # ==================== 用户模块 MySQL 异步 ORM 配置 ====================
    # 优先使用完整 SQLAlchemy URL；为空时使用下方 MYSQL_* 配置拼接。
    # 示例：mysql+aiomysql://root:1234@localhost:3306/nl2sql?charset=utf8mb4
    user_database_url: str = Field(default="", alias="USER_DATABASE_URL")
    mysql_host: str = Field(default="127.0.0.1", alias="MYSQL_HOST")
    mysql_port: int = Field(default=3306, alias="MYSQL_PORT")
    mysql_user: str = Field(default="root", alias="MYSQL_USER")
    mysql_password: str = Field(default="", alias="MYSQL_PASSWORD")
    mysql_database: str = Field(default="nl2sql", alias="MYSQL_DATABASE")
    mysql_charset: str = Field(default="utf8mb4", alias="MYSQL_CHARSET")
    mysql_pool_size: int = Field(default=10, alias="MYSQL_POOL_SIZE")
    mysql_max_overflow: int = Field(default=20, alias="MYSQL_MAX_OVERFLOW")
    sql_echo: bool = Field(default=False, alias="SQL_ECHO")

    # ==================== NL2SQL 业务查询 MySQL 数据源配置 ====================
    # 注意：这里是用户自然语言查询要访问的业务库，不保存用户、历史会话等系统数据。
    mysql_query_enabled: bool = Field(default=True, alias="MYSQL_QUERY_ENABLED")
    mysql_query_name: str = Field(default="mysql_local", alias="MYSQL_QUERY_NAME")
    mysql_query_host: str = Field(default="127.0.0.1", alias="MYSQL_QUERY_HOST")
    mysql_query_port: int = Field(default=3306, alias="MYSQL_QUERY_PORT")
    mysql_query_user: str = Field(default="root", alias="MYSQL_QUERY_USER")
    mysql_query_password: str = Field(default="", alias="MYSQL_QUERY_PASSWORD")
    mysql_query_database: str = Field(default="", alias="MYSQL_QUERY_DATABASE")
    mysql_query_charset: str = Field(default="utf8mb4", alias="MYSQL_QUERY_CHARSET")
    mysql_query_description: str = Field(default="本地 MySQL 业务数据库", alias="MYSQL_QUERY_DESCRIPTION")

    # ==================== 元数据摘要与 Prompt 压缩 ====================
    # 控制是否对数据库元数据（表结构信息）生成简短摘要
    # # 未启用时，发给 LLM 的原始元数据（很长）：
    # {
    #   "name": "employees",
    #   "columns": [
    #     {"name": "id", "type": "INTEGER", "comment": "员工ID"},
    #     {"name": "name", "type": "TEXT", "comment": "姓名"},
    #     {"name": "dept", "type": "TEXT", "comment": "所属部门"},
    #     {"name": "salary", "type": "REAL", "comment": "薪资"},
    #     ...很多字段
    #   ]
    # }
    #
    # # 启用后，多了 summary 字段（压缩版）：
    # {
    #   "name": "employees",
    #   "summary": "员工信息表，含部门、薪资、经理关系",  ← 一句话概括
    #   "columns": [...]
    # }
    # 好处：表很多、字段很多时，Prompt 会非常长。加上摘要后 LLM 能更快理解表的用途，同时减少 token 消耗。
    metadata_summary_enabled: bool = Field(default=True, alias="METADATA_SUMMARY_ENABLED")
    # 控制是否在对话中使用 LLM 生成元数据摘要
    # False（默认）规则生成：直接拼接表名+注释+字段名
    # chat_module.py 中的使用
    metadata_summary_use_llm_in_chat: bool = Field(default=False, alias="METADATA_SUMMARY_USE_LLM_IN_CHAT")
    # 元数据摘要缓存路径
    metadata_summary_cache_path: str = Field(
        default="./data/metadata_summaries.json",
        alias="METADATA_SUMMARY_CACHE_PATH",
    )
    postgres_metadata_cache_ttl_seconds: float = Field(default=60.0, alias="POSTGRES_METADATA_CACHE_TTL_SECONDS")

    # ==================== REST API 查询数据源配置 ====================
    rest_api_enabled: bool = Field(default=False, alias="REST_API_ENABLED")
    rest_api_name: str = Field(default="rest_api_demo", alias="REST_API_NAME")
    rest_api_url: str = Field(default="", alias="REST_API_URL")
    rest_api_table_name: str = Field(default="api_records", alias="REST_API_TABLE_NAME")
    rest_api_data_path: str = Field(default="", alias="REST_API_DATA_PATH")
    rest_api_description: str = Field(default="REST API JSON 数据源", alias="REST_API_DESCRIPTION")
    rest_api_headers_json: str = Field(default="{}", alias="REST_API_HEADERS_JSON")
    rest_api_query_params_json: str = Field(default="{}", alias="REST_API_QUERY_PARAMS_JSON")
    rest_api_key_param: str = Field(default="", alias="REST_API_KEY_PARAM")
    rest_api_api_key: str = Field(default="", alias="REST_API_API_KEY")
    rest_api_timeout: float = Field(default=10.0, alias="REST_API_TIMEOUT")
    rest_api_cache_ttl_seconds: float = Field(default=60.0, alias="REST_API_CACHE_TTL_SECONDS")
    rest_api_service_mode: str = Field(default="table", alias="REST_API_SERVICE_MODE")

    # ==================== GraphQL 查询数据源配置 ====================
    graphql_enabled: bool = Field(default=True, alias="GRAPHQL_ENABLED")
    graphql_name: str = Field(default="countries_graphql", alias="GRAPHQL_NAME")
    graphql_endpoint: str = Field(
        default="https://countries.trevorblades.com/graphql",
        alias="GRAPHQL_ENDPOINT",
    )
    graphql_table_name: str = Field(default="countries", alias="GRAPHQL_TABLE_NAME")
    graphql_data_path: str = Field(default="data.countries", alias="GRAPHQL_DATA_PATH")
    graphql_headers_json: str = Field(default="{}", alias="GRAPHQL_HEADERS_JSON")
    graphql_variables_json: str = Field(default="{}", alias="GRAPHQL_VARIABLES_JSON")
    graphql_query: str = Field(default="", alias="GRAPHQL_QUERY")
    graphql_timeout: float = Field(default=10.0, alias="GRAPHQL_TIMEOUT")
    graphql_cache_ttl_seconds: float = Field(default=3600.0, alias="GRAPHQL_CACHE_TTL_SECONDS")
    graphql_description: str = Field(
        default="公开 Countries GraphQL 国家数据源（国家、首都、货币、洲、语言）",
        alias="GRAPHQL_DESCRIPTION",
    )

    # ==================== PostgreSQL 查询数据源配置 ====================
    postgres_query_enabled: bool = Field(default=False, alias="POSTGRES_QUERY_ENABLED")
    postgres_query_name: str = Field(default="postgres_local", alias="POSTGRES_QUERY_NAME")
    postgres_query_host: str = Field(default="127.0.0.1", alias="POSTGRES_QUERY_HOST")
    postgres_query_port: int = Field(default=5432, alias="POSTGRES_QUERY_PORT")
    postgres_query_user: str = Field(default="postgres", alias="POSTGRES_QUERY_USER")
    postgres_query_password: str = Field(default="", alias="POSTGRES_QUERY_PASSWORD")
    postgres_query_database: str = Field(default="", alias="POSTGRES_QUERY_DATABASE")
    postgres_query_schema: str = Field(default="public", alias="POSTGRES_QUERY_SCHEMA")
    postgres_query_sslmode: str = Field(default="", alias="POSTGRES_QUERY_SSLMODE")
    postgres_query_description: str = Field(default="PostgreSQL 业务数据库", alias="POSTGRES_QUERY_DESCRIPTION")

    # ==================== 高斯数据库查询数据源配置 ====================
    # 高斯常见部署兼容 PostgreSQL 协议，MVP 阶段复用 PostgreSQLAdapter。
    gauss_query_enabled: bool = Field(default=False, alias="GAUSS_QUERY_ENABLED")
    gauss_query_name: str = Field(default="gauss_local", alias="GAUSS_QUERY_NAME")
    gauss_query_host: str = Field(default="127.0.0.1", alias="GAUSS_QUERY_HOST")
    gauss_query_port: int = Field(default=5432, alias="GAUSS_QUERY_PORT")
    gauss_query_user: str = Field(default="", alias="GAUSS_QUERY_USER")
    gauss_query_password: str = Field(default="", alias="GAUSS_QUERY_PASSWORD")
    gauss_query_database: str = Field(default="", alias="GAUSS_QUERY_DATABASE")
    gauss_query_schema: str = Field(default="public", alias="GAUSS_QUERY_SCHEMA")
    gauss_query_description: str = Field(default="高斯数据库业务数据源", alias="GAUSS_QUERY_DESCRIPTION")

    # ==================== Hive / Hadoop 查询数据源配置 ====================
    # Hadoop 本身是分布式存储/计算生态；NL2SQL 通常通过 HiveServer2 执行 SQL。
    hive_query_enabled: bool = Field(default=False, alias="HIVE_QUERY_ENABLED")
    hive_query_name: str = Field(default="hive_local", alias="HIVE_QUERY_NAME")
    hive_query_host: str = Field(default="127.0.0.1", alias="HIVE_QUERY_HOST")
    hive_query_port: int = Field(default=10000, alias="HIVE_QUERY_PORT")
    hive_query_user: str = Field(default="", alias="HIVE_QUERY_USER")
    hive_query_password: str = Field(default="", alias="HIVE_QUERY_PASSWORD")
    hive_query_database: str = Field(default="default", alias="HIVE_QUERY_DATABASE")
    hive_query_auth: str = Field(default="NOSASL", alias="HIVE_QUERY_AUTH")
    hive_query_description: str = Field(default="Hive/Hadoop 数仓数据源", alias="HIVE_QUERY_DESCRIPTION")
    hive_query_mode: str = Field(default="server", alias="HIVE_QUERY_MODE")
    hive_demo_csv_path: str = Field(default="./data/hadoop_order_events.csv", alias="HIVE_DEMO_CSV_PATH")
    hive_demo_data_dir: str = Field(default="./data/hadoop", alias="HIVE_DEMO_DATA_DIR")

    # ==================== 达梦数据库查询数据源配置 ====================
    dameng_query_enabled: bool = Field(default=False, alias="DAMENG_QUERY_ENABLED")
    dameng_query_name: str = Field(default="dameng_local", alias="DAMENG_QUERY_NAME")
    dameng_query_host: str = Field(default="127.0.0.1", alias="DAMENG_QUERY_HOST")
    dameng_query_port: int = Field(default=5236, alias="DAMENG_QUERY_PORT")
    dameng_query_user: str = Field(default="", alias="DAMENG_QUERY_USER")
    dameng_query_password: str = Field(default="", alias="DAMENG_QUERY_PASSWORD")
    dameng_query_schema: str = Field(default="", alias="DAMENG_QUERY_SCHEMA")
    dameng_jdbc_driver_path: str = Field(default="", alias="DAMENG_JDBC_DRIVER_PATH")
    dameng_query_description: str = Field(default="达梦数据库业务数据源", alias="DAMENG_QUERY_DESCRIPTION")

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
    audit_db_path: str = Field(default="data/audit.db", alias="AUDIT_DB_PATH")
    audit_result_sample_rows: int = Field(default=50, alias="AUDIT_RESULT_SAMPLE_ROWS")

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

if __name__ == "__main__":

    print(f"✅ 配置加载成功")
    print(f"   当前 Provider : {settings.llm_provider}")
    print(f"   当前模型      : {settings.llm_model}")
    print(f"   Base URL      : {settings.llm_base_url}")
    print(f"   API Key 已配置: {'是' if settings.llm_api_key else '否'}")

