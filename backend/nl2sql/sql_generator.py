"""
SQL 生成器 - 调用 LLM（公司内部 LiteLLM 或 DashScope）生成 SQL
支持 Self-Correction（执行失败后自动修复）
"""
import httpx
import re
from typing import Dict, Any, Optional, Tuple
from loguru import logger
from backend.config.config import settings
from backend.nl2sql.prompt_builder import PromptBuilder


class SQLGenerator:
    def __init__(self):
        self.prompt_builder = PromptBuilder()
        # 使用较长的超时，复杂 SQL 生成可能需要时间
        self.client = httpx.Client(timeout=getattr(settings, 'llm_timeout', 15))
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key

    async def generate_sql(self, question: str, metadata: Dict[str, Any],
                           dialect: str = "sqlite") -> Tuple[str, str, Optional[str]]:
        """
        生成 SQL
        返回: (sql, thought, error)
        """
        # 1. 构建 Prompt（当前 MVP 使用全量 schema，后续可做表相关性选择）
        prompt = self.prompt_builder.build_prompt(question, metadata)

        try:
            # 调用 OpenAI 兼容接口（LiteLLM / DashScope 都支持）
            # {
            #     "Content-Type": "application/json",  告诉服务器：我发的数据是 JSON 格式
            #     "Authorization": "Bearer sk-xxxxxxxxxxxx"  告诉服务器：这是我的 API 密钥，验证我有权限调用
            # }
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
                        {"role": "system", "content": "你是一个专业的SQL生成助手，严格遵守只读规则。"},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1,  # 低温度，更稳定、更少幻觉 控制随机性
                    "max_tokens": 2048,  # 最大输出长度
                    "stream": False  # 是否流式返回
                },
                headers=headers
            )
            # 1. 检查HTTP状态码，非200就抛异常
            response.raise_for_status()
            # 2. 解析JSON响应
            data = response.json()
            # 3. 提取LLM生成的SQL
            # {
            #   "choices": [
            #     {
            #       "message": {
            #         "content": "需要查询技术部薪资最高的员工，涉及 employees...
            content = data["choices"][0]["message"]["content"]

            print(content)

            # 提取思考过程和 SQL
            # 4. 提取思考过程和 SQL
            # 用正则从内容中分离出"思考过程"和"SQL
            thought, sql = self._extract_thought_and_sql(content)

            # 5. 检查是否成功提取 SQL
            if not sql:
                return "", thought or content, "未能从LLM响应中提取到有效SQL"

            # 6. 清理 SQL
            # 6. 清理SQL（去markdown、只保留第一条语句）
            sql = self._clean_sql(sql)

            # 7. 返回结果
            return sql, thought, None

        except httpx.HTTPStatusError as e:
            logger.error(f"LLM HTTP error: {e}")
            error_detail = e.response.text if e.response else str(e)
            return "", "", f"LLM服务调用失败: {error_detail}"
        except Exception as e:
            logger.error(f"LLM generation error: {e}")
            return "", "", f"SQL生成异常: {str(e)}"

    def _extract_thought_and_sql(self, content: str) -> Tuple[str, str]:
        """从 LLM 响应中提取思考过程和 SQL 代码块"""
        thought = ""
        sql = ""

        # 优先提取 ```sql ... ``` 块
        sql_match = re.search(r'```sql\s*(.*?)\s*```', content, re.DOTALL | re.IGNORECASE)
        if sql_match:
            sql = sql_match.group(1).strip()

        # 提取思考部分（SQL 前面的内容）
        if sql_match:
            thought = content[:sql_match.start()].strip()
        else:
            # 如果没有代码块，尝试找 SELECT 开头
            select_match = re.search(r'(SELECT\s+.*)', content, re.DOTALL | re.IGNORECASE)
            if select_match:
                sql = select_match.group(1).strip()
                thought = content[:select_match.start()].strip()
            else:
                thought = content

        return thought, sql

    def _clean_sql(self, sql: str) -> str:
        """清理 SQL 中的多余内容"""
        # 去掉多余的 markdown
        sql = re.sub(r'```.*?```', '', sql, flags=re.DOTALL)
        sql = sql.strip()
        # 只保留第一个语句
        if ';' in sql:
            sql = sql.split(';')[0] + ';'
        return sql.strip()

    async def self_correct_sql(self, original_sql: str, error_msg: str,
                               question: str, metadata: Dict[str, Any],
                               dialect: str = "sqlite") -> Tuple[str, str]:
        """
        Self-Correction: 让 LLM 根据执行错误修复 SQL
        """
        correction_prompt = f"""之前的SQL执行失败了，请根据错误信息修复。

用户原始问题：{question}

之前生成的SQL：
```sql
{original_sql}
```

执行错误信息：
{error_msg}

请分析错误原因，然后生成修正后的正确SQL。保持只读规则（只允许 SELECT / WITH）。
只输出修正后的SQL（用```sql包裹）和简短修复说明。"""

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
                    "messages": [{"role": "user", "content": correction_prompt}],
                    "temperature": 0.0,
                    "max_tokens": 1024
                },
                headers=headers
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]

            _, corrected_sql = self._extract_thought_and_sql(content)
            corrected_sql = self._clean_sql(corrected_sql)

            return corrected_sql, content[:300]  # 返回部分思考
        except Exception as e:
            logger.error(f"Self-correction failed: {e}")
            return "", str(e)
