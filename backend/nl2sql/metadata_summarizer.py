"""
元数据摘要/压缩机制 - 使用 LLM 生成表和字段的简短业务描述
用于减少 Prompt 长度（特别是对于大 schema 或小模型），提升 NL2SQL 效果
支持预生成摘要缓存，后续可持久化到文件或DB
# 原始元数据（发给 LLM 的内容）—— 非常长！

表1: employees
  字段: id (INTEGER, 员工ID), name (TEXT, 姓名), dept (TEXT, 所属部门),
        salary (REAL, 薪资), manager_id (INTEGER, 直属经理ID),
        hire_date (DATE, 入职日期), email (TEXT, 邮箱), phone (TEXT, 电话)...
  注释: 员工信息表，包含自关联经理关系

表2: departments
  字段: id (INTEGER, 部门ID), name (TEXT, 部门名称), parent_id (INTEGER, 上级部门ID),
        manager_id (INTEGER, 部门负责人ID), budget (REAL, 部门预算)...
  注释: 部门信息表

表3: sales
  字段: id, product_id, customer_id, amount, quantity, sale_date, region, channel...
  注释: 销售记录表，用于统计分析

... 可能还有 20 张表，每张表 10+ 个字段

用户问题: "技术部薪资高于平均的员工有哪些？"
    问题：
    LLM 的 Prompt 有长度限制，表太多可能塞不下
    即使塞得下，LLM 要从大量字段中找到相关的，容易分心、搞错
    token 越多，费用越高、速度越慢

# 压缩后的元数据 —— 简洁明了！

表1: employees
  摘要: "员工信息表，含部门、薪资、经理关系"    ← 一句话告诉 LLM 这张表是干嘛的
  字段: id, name, dept, salary, manager_id...

表2: departments
  摘要: "部门信息表，含层级和预算"

表3: sales
  摘要: "销售记录表，含产品、客户、金额、区域"

用户问题: "技术部薪资高于平均的员工有哪些？"
    LLM 看到摘要后，一眼就知道：
    employees 表有"薪资"和"部门" → 相关！
    departments 表是部门信息 → 可能相关
    sales 表是销售 → 不相关，跳过

摘要是给 LLM 的"快速预览"，用一两句话概括每张表是干嘛的，
让 LLM 不用读完所有字段就能判断哪些表和问题相关，
从而减少 Prompt 长度、提高准确率、节省费用。
"""
import hashlib
import httpx
import json
from typing import Dict, Any, List, Optional
from pathlib import Path
from loguru import logger
from backend.config.config import settings


class MetadataSummarizer:
    """元数据摘要器，生成表和字段的简短业务描述。"""

    # 自动从 .env 读取 LLM 配置
    # 自动从 ./data/metadata_summaries.json 加载缓存
    def __init__(self, cache_path: Optional[str] = None):
        self.client = httpx.Client(timeout=getattr(settings, 'llm_timeout', 120))
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.cache_path = Path(cache_path or settings.metadata_summary_cache_path)
        self.cache: Dict[str, Dict[str, Any]] = {}
        self._load_cache()

    # 从本地 JSON 文件读取已生成的摘要缓存。
    def _load_cache(self) -> None:
        """从本地 JSON 文件加载摘要缓存。"""
        if not self.cache_path.exists():
            self.cache = {}
            return

        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(self.cache, dict):
                self.cache = {}
        except Exception as exc:
            logger.warning(f"Load metadata summary cache failed: {exc}")
            self.cache = {}

    # 把摘要缓存写入本地 JSON 文件。
    def _save_cache(self) -> None:
        """把摘要缓存写入本地 JSON 文件。"""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self.cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"Save metadata summary cache failed: {exc}")

    # 根据表名和字段结构生成一个唯一的缓存 key。
    def _build_cache_key(self, data_source: str, table: Dict[str, Any]) -> str:
        """按数据源、表名和字段结构生成缓存 key，字段变化后自动失效。"""
        # 第一次查询 employees 表
        # sqlite_demo:employees:a3f5c8
        # 缓存没有 → 生成摘要 → 存入缓存

        # 再次查询同样的 employees 表
        # sqlite_demo:employees:a3f5c8
        # 缓存命中 → 直接返回，不重新生成

        # 给 employees 表加了新字段
        # sqlite_demo:employees:b7e2d9
        # 哈希变了 → 缓存未命中 → 重新生成摘要
        signature = {
            "name": table.get("name", ""),
            "comment": table.get("comment", ""),
            "columns": [
                {
                    "name": col.get("name", ""),
                    "type": col.get("type", ""),
                    "comment": col.get("comment", ""),
                }
                for col in table.get("columns", [])
            ],
        }
        # sort_keys=True 确保每次生成的 JSON 键的顺序一致，这样同样的表结构永远产生同样的字符串。
        # → '{"columns": [...], "comment": "员工信息表", "name": "employees"}
        raw = json.dumps(signature, ensure_ascii=False, sort_keys=True)
        # → "a3f5c8d2e1b4"  (固定12位字符串)
        # 同样的表结构 → 同样的哈希值，字段改了 → 哈希值就变。
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        # → "sqlite_demo:employees:a3f5c8d2e1b4"
        return f"{data_source}:{table.get('name', '')}:{digest}"

    # 当 LLM 调用失败时，生成一个保守的摘要，保证聊天链路稳定可用。
    def _fallback_summary(self, table_name: str, columns: List[Dict], comment: str = "") -> str:
        """不调用 LLM 时的保守摘要，保证聊天链路稳定可用。"""
        # 取前6个字段名
        key_columns = [col.get("name", "") for col in columns[:6] if col.get("name")]
        # 用表的注释 + 字段名拼一句话
        base = comment or f"表 {table_name} 用于存储相关业务数据"
        # 表名: employees
        # 注释: 员工信息表，包含自关联经理关系
        # 字段: id, name, dept, salary, manager_id, hire_date, email, phone...
        if key_columns:
            return f"{base}；关键字段包括：{', '.join(key_columns)}。"
        # "员工信息表，包含自关联经理关系；关键字段包括：id, name, dept, salary, manager_id, hire_date。"
        return f"{base}。"

    # 调用 LLM 生成摘要。
    def _call_llm(self, prompt: str) -> str:
        """内部调用 LLM"""
        try:
            headers = {
                "Content-Type": "application/json",
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = self.client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "你是一个数据库 schema 专家，擅长生成简洁的业务描述。"},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 200,
                    "stream": False
                },
                headers=headers
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"MetadataSummarizer LLM call failed: {e}")
            return ""

    # 为单个表生成简短摘要（1-2句业务描述 + 关键字段含义）
    # 为一张表生成摘要，优先用缓存，缓存没有再生成。
    async def summarize_table(
        self,
        table_name: str,
        columns: List[Dict],
        comment: str = "",
        *,
        data_source: str = "default",
        table: Optional[Dict[str, Any]] = None,
        refresh: bool = False,
        use_llm: bool = True,
    ) -> str:
        """
        为单个表生成简短摘要（1-2句业务描述 + 关键字段含义）
        """
        table_for_key = table or {"name": table_name, "comment": comment, "columns": columns}
        cache_key = self._build_cache_key(data_source, table_for_key)
        cached = self.cache.get(cache_key)
        # 缓存中有这条记录？
        #   ├─ 否 → 重新生成
        #   └─ 是 ↓
        #      强制刷新(refresh=True)？
        #        ├─ 是 → 重新生成
        #        └─ 否 ↓
        #           use_llm=False？
        #             ├─ 是 → ✅ 直接用缓存（规则版也够用）
        #             └─ 否 ↓
        #                缓存是 LLM 生成的？
        #                  ├─ 是 → ✅ 用缓存（高质量版本）
        #                  └─ 否 →  重新用 LLM 生成（规则版升级为 LLM 版）
        if cached and not refresh and (not use_llm or cached.get("generated_by") == "llm"):
            # not use_llm → True，或者 cached.get("generated_by") == "llm"
            # LLM 生成的
            # "llm"
            # ✅ 用缓存
            # 质量高，值得复用

            # 规则生成的
            # "fallback"
            # ❌ 不用缓存
            # 用户要求用 LLM，规则版质量不够，重新生成
            return str(cached.get("summary") or "")

        if not use_llm:
            summary = self._fallback_summary(table_name, columns, comment)
            self.cache[cache_key] = {
                "data_source": data_source,
                "table_name": table_name,
                "summary": summary,
                "generated_by": "fallback",
            }
            self._save_cache()
            return summary

        col_names = [col['name'] for col in columns]
        col_types = [f"{col['name']}({col['type']})" for col in columns[:5]]  # 限制前5个避免太长

        prompt = f"""请为以下数据库表生成一个简洁的业务描述（1-2句话），重点说明表的作用和关键字段含义。用于 NL2SQL 的 Prompt 优化。
表名: {table_name}
表注释: {comment or '无'}
字段: {', '.join(col_names)}
字段类型示例: {', '.join(col_types)}

输出格式：仅返回描述文本，不要解释。"""

        summary = self._call_llm(prompt)
        if summary:
            self.cache[cache_key] = {
                "data_source": data_source,
                "table_name": table_name,
                "summary": summary,
                "generated_by": "llm",
            }
        else:
            # 回退到简单描述
            summary = self._fallback_summary(table_name, columns, comment)
            self.cache[cache_key] = {
                "data_source": data_source,
                "table_name": table_name,
                "summary": summary,
                "generated_by": "fallback",
            }
        self._save_cache()
        return summary

    # 遍历所有表，为每张表生成摘要，返回压缩后的完整元数据。
    async def summarize_metadata(
        self,
        metadata: Dict[str, Any],
        *,
        data_source: str = "default",
        refresh: bool = False,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        """
        为整个 metadata 生成摘要版本
        返回类似原 metadata 但每个表有 'summary' 字段，columns 精简
        """
        summarized = {"tables": [], "total_tables": metadata.get("total_tables", 0)}
        
        for table in metadata.get("tables", []):
            table_name = table["name"]
            summary = await self.summarize_table(
                table_name, 
                table.get("columns", []), 
                table.get("comment", ""),
                data_source=data_source,
                table=table,
                refresh=refresh,
                use_llm=use_llm,
            )
            
            # 精简 columns，只保留 name, type, comment（如果有）
            slim_columns = []
            for col in table.get("columns", []):
                slim_col = {
                    "name": col["name"],
                    "type": col["type"],
                }
                if col.get("comment"):
                    slim_col["comment"] = col["comment"]
                slim_columns.append(slim_col)
            
            summarized_table = {
                "name": table_name,
                "comment": table.get("comment", ""),
                "summary": summary,
                "columns": slim_columns,
                "primary_key": table.get("primary_key", []),
                "foreign_keys": table.get("foreign_keys", [])
            }
            summarized["tables"].append(summarized_table)
        
        return summarized

    # 根据表名查找缓存中的摘要。
    def get_cached_summary(self, table_name: str) -> Optional[str]:
        for item in self.cache.values():
            if item.get("table_name") == table_name:
                return item.get("summary")
        return None

    # 清空内存缓存并删除缓存文件。
    def clear_cache(self):
        self.cache.clear()
        self._save_cache()
