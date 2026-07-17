"""
简单表相关性选择器
根据用户问题中的关键词，筛选最相关的表，减少 Prompt 长度
（后续可升级为 embedding + 向量检索）
"""
from typing import Dict, List, Any
import re
from loguru import logger

try:
    # Python 最流行的中文分词工具，能把一句中文拆成有意义的词语。
    import jieba
except ImportError:  # pragma: no cover - 兼容未安装 jieba 的最小运行环境
    jieba = None


def tokenize_question(question: str) -> List[str]:
    """将问题拆成可用于匹配的关键词，jieba 不可用时使用保守降级策略。"""
    question = question.lower()
    if jieba:
        return [word for word in jieba.lcut(question) if len(word) > 1]

    ascii_words = re.findall(r"[a-zA-Z0-9_]+", question)
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", question)
    return ascii_words + chinese_chunks


def select_relevant_tables(
    question: str, 
    metadata: Dict[str, Any], 
    max_tables: int = 5
) -> List[str]:
    """
    基于关键词匹配选择相关表
    返回表名列表
        question = "技术部薪资高于平均的员工有哪些？"
        metadata = {
            "tables": [
                {"name": "employees", "comment": "员工信息表，包含自关联经理关系", "columns": [...]},
                {"name": "departments", "comment": "部门信息表", "columns": [...]},
                {"name": "sales", "comment": "销售记录表，用于统计分析", "columns": [...]}
            ]
        }
    """
    tables = metadata.get("tables", [])
    if not tables:
        return []

    question_lower = question.lower()
    
    # 简单关键词权重
    scores = {}
    for table in tables:
        name = table["name"].lower()
        comment = (table.get("comment") or "").lower()
        score = 0
        
        # 表名完全匹配
        # 表名整个出现在问题中
        if name in question_lower:
            score += 10
        
        # 表名部分匹配
        # 表名按 _ 或空格拆分后，某一段出现在问题中
        for part in re.split(r'[_\s]', name):
            if part and part in question_lower:
                score += 3

        # 把问题拆成有意义的词 长度大于一的词
        words = tokenize_question(question_lower)
        # "技术部薪资高于平均的员工有哪些？"
        # → ["技术部", "薪资", "高于", "平均", "的", "员工", "有哪些"]

        # 然后逐个检查
        if comment and any(w in comment for w in words):
            score += 2
        
        # 字段名匹配
        # 字段名整个出现在问题中
        for col in table.get("columns", []):
            col_name = col["name"].lower()
            if col_name in question_lower:
                score += 1
        
        scores[table["name"]] = score

    # 按分数排序，取 top-k 降序
    sorted_tables = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    # 至少保留有分数的表，最多 max_tables
    # [:max_tables] → 最多取前 max_tables 个（默认 5 个）
    selected = [name for name, score in sorted_tables if score > 0][:max_tables]
    
    # 如果一个都没匹配到，就返回所有表（保底）
    if not selected:
        selected = [t["name"] for t in tables][:max_tables]
    
    logger.info(f"表选择结果: {selected} (原始表数: {len(tables)})")
    return selected
