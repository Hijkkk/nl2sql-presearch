"""
Pydantic 数据模型
定义所有 API 的请求体和响应体
    自动数据验证：字段类型、长度、必填项都会自动检查，非法请求直接返回 422。
    自动生成 OpenAPI 文档：访问 /docs 时，Swagger 界面会非常清晰。
    类型安全：IDE 有完整提示，代码更易维护。
    序列化方便：直接返回模型对象，FastAPI 会自动转 JSON。
"""
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Dict, Any, Optional
from datetime import datetime

"""
定义前端发给后端的请求格式
    // 前端发送的 JSON
    {
      "question": "查询IT部门有多少员工",
      "data_source": "sqlite_demo",
      "session_id": "abc123"
    }
    question：用户的自然语言问题（必填，3-500字）
    data_source：用哪个数据库（默认 sqlite_demo）
    session_id：会话ID（可选，用于多轮对话）
"""

class ChatRequest(BaseModel):
    """自然语言查询请求"""
    model_config = ConfigDict(populate_by_name=True)

    question: str = Field(..., min_length=3, max_length=500, description="自然语言问题")
    data_source: str = Field(default="sqlite_demo", description="数据源名称")
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    model_id: Optional[str] = None
    model_conf: Optional[Dict[str, Any]] = Field(default=None, alias="model_config")
    client_location: Optional[Dict[str, Any]] = Field(
        default=None,
        description="浏览器 Geolocation API 返回的用户位置，例如 {latitude, longitude, accuracy}",
    )

"""
定义后端返回给前端的响应格式
    success：是否成功
    question：回显用户的问题
    sqlLLM ：生成的 SQL
    results：查询结果（字典列表）
    columns：列名列表
    row_count：返回行数
    execution_time：执行耗时（秒）
    llm_thoughtLLM ：的思考过程
    insight：数据洞察/总结
    error：错误信息（失败时）
    corrected_sql：自纠错后的 SQL（如果有
"""
class ChatResponse(BaseModel):
    """查询响应"""
    success: bool
    question: str
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    sql: Optional[str] = None
    results: Optional[List[Dict[str, Any]]] = None
    columns: Optional[List[str]] = None
    row_count: Optional[int] = None
    execution_time: Optional[float] = None  # 秒
    llm_thought: Optional[str] = None
    answer: Optional[str] = None
    insight: Optional[str] = None
    error: Optional[str] = None
    corrected_sql: Optional[str] = None  # 如果有自修复
    stage_timings: Optional[Dict[str, float]] = None

    memory_hit: bool = False
"""
返回数据库的表结构信息（前端用来展示"有哪些表、哪些字段"）
    {
      "data_source": "sqlite_demo",
      "tables": [
        {
          "name": "employees",
          "columns": ["id", "name", "dept", "salary"],
          "comment": "员工信息表"
        }
      ],
      "total_tables": 3,
      "generated_at": "2026-07-14T10:30:00"
    }
"""
class MetadataResponse(BaseModel):
    """元数据响应"""
    data_source: str
    tables: List[Dict[str, Any]]  # {name, columns: [...], comment, foreign_keys?}
    total_tables: int
    generated_at: datetime

# 描述一个数据源的连接状态
class DataSourceInfo(BaseModel):
    """数据源信息"""
    name: str
    type: str  # sqlite, mysql, postgresql, rest_api, hive
    status: str  # connected, error
    description: str

# 记录每次查询的审计信息（谁、问了什么、生成了什么 SQL、结果如何）
"""
{
  "id": 1,
  "timestamp": "2026-07-14T10:30:00",
  "user": "demo_user",
  "question": "查询IT部门员工",
  "generated_sql": "SELECT * FROM employees WHERE dept='IT'",
  "executed_sql": "SELECT * FROM employees WHERE dept='IT' LIMIT 1000",
  "data_source": "sqlite_demo",
  "row_count": 15,
  "status": "success",
  "error_message": null
}
"""
class AuditLog(BaseModel):
    """审计日志（预留）"""
    id: Optional[int] = None
    timestamp: datetime
    user: str = "demo_user"
    question: str
    generated_sql: str
    executed_sql: str
    data_source: str
    row_count: int
    status: str  # success, failed, blocked
    error_message: Optional[str] = None
