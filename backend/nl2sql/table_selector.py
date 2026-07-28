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


def extract_chinese_bigrams(text: str) -> set[str]:
    """提取中文二字片段，用于处理“销售总额” vs “销售记录”这类近似匹配。"""
    bigrams: set[str] = set()
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        bigrams.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return bigrams


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

    # 问题小写化 "查询IT部门".lower()   →  "查询it部门"
    question_lower = question.lower()
    # 把问题拆成有意义的词，长度大于一的词
    # ("技术部薪资高于平均的员工有哪些？")
    # → ["技术部", "薪资", "高于", "平均", "的", "员工", "有", "哪些", "？"]
    question_words = tokenize_question(question_lower)
    # 提取所有连续的两个汉字组合（二元组）
    # "技术部薪资高于平均的员工有哪些"

    # → 逐字滑动窗口，每次取2个字：
    #
    # "技术", "术部", "部薪", "薪资", "资高", "高于",
    # "于平", "平均", "均的", "的员", "员工", "工有",
    # "有哪", "哪些"
    #
    # → {"技术", "术部", "部薪", "薪资", "资高", "高于",
    #     "于平", "平均", "均的", "的员", "员工", "工有",
    #     "有哪", "哪些"}

    question_bigrams = extract_chinese_bigrams(question_lower)
    
    # 简单关键词权重
    scores = {}
    for table in tables:
        # 名字
        name = table["name"].lower()
        # 描述
        comment = (table.get("comment") or "").lower()
        # 概括
        summary = (table.get("summary") or "").lower()
        # "tables": [
        #     {"name": "employees", "comment": "员工信息表，包含自关联经理关系", "columns": [...]},
        #     {"name": "departments", "comment": "部门信息表", "columns": [...]},
        #     {"name": "sales", "comment": "销售记录表，用于统计分析", "columns": [...]}
        # ]
        # column_text = "id 员工id name 姓名
        # dept 所属部门 salary 薪资 manager_id 直属经理id"
        column_text = " ".join(
            f"{col.get('name', '')} {col.get('comment', '')}".lower()
            for col in table.get("columns", [])
        )
        # "employees 员工信息表，包含自关联经理关系  id 员工id name 姓名
        # dept 所属部门 salary 薪资 manager_id 直属经理id"
        searchable_text = f"{name} {comment} {summary} {column_text}"
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
        words = question_words
        # "技术部薪资高于平均的员工有哪些？"
        # → ["技术部", "薪资", "高于", "平均", "的", "员工", "有哪些"]

        # 然后逐个检查
        for word in words:
            if word in searchable_text:
                score += 2
        # 表的文本中所有连续两字组合的集合
        table_bigrams = extract_chinese_bigrams(searchable_text)
        # question_bigrams 用户问题中所有连续两字组合的集合
        # len(...) 集合求交集（两边都有的二元组） & 交集有多少个
        # min(..., 3) 最多贡献 3 分（防止某个表靠二元组刷太高分）
        score += min(len(question_bigrams & table_bigrams), 3)
        
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
    
    hadoop_tables = [
        "hadoop_order_events",
        "hadoop_user_profiles",
        "hadoop_product_dim",
        "hadoop_region_dim",
    ]
    available_hadoop_tables = [name for name in hadoop_tables if name in {t["name"] for t in tables}]
    hadoop_keywords = (
        "hadoop", "hdfs", "订单", "销售", "成交", "gmv", "金额", "城市", "省份",
        "用户", "vip", "商品", "品牌", "品类", "销量", "月", "趋势", "大区"
    )
    if available_hadoop_tables and (
        any(table_name in selected for table_name in available_hadoop_tables)
        or any(keyword in question_lower for keyword in hadoop_keywords)
    ):
        selected = list(dict.fromkeys(available_hadoop_tables + selected))[:max_tables]

    police_table_groups = {
        "addr_alias": ("别名", "曾用名", "民间称呼", "地址别名", "包含"),
        "addr_standard_address": ("标准地址", "完整地址", "地址", "经度", "纬度", "街道", "社区"),
        "person_basic": ("人员", "人口", "姓名", "曾用名", "证件", "户籍"),
        "person_address_relation": ("住址", "居住", "当前住", "人房", "租住", "房屋"),
        "house_info": ("房屋", "出租", "自住", "空置", "产权人", "危险房屋"),
        "police_alert": ("警情", "报警", "接警", "报警内容", "案发", "民警"),
        "alert_caller": ("报警人", "来电人", "实名报警", "目击者"),
        "alert_address_relation": ("案发地", "报警地址", "嫌疑人藏身地", "涉及地址"),
        "organization_info": ("单位", "组织", "企业", "法人", "行业"),
        "organization_address_relation": ("单位地址", "经营地址", "注册地址"),
    }
    available_police_tables = {table["name"] for table in tables}
    police_selected = [
        table_name
        for table_name, keywords in police_table_groups.items()
        if table_name in available_police_tables and any(keyword in question_lower for keyword in keywords)
    ]
    if police_selected:
        police_priority: list[str] = []
        if any(keyword in question_lower for keyword in ("人员", "人口", "姓名", "当前住", "住址", "居住")):
            police_priority.extend(["person_basic", "person_address_relation", "house_info", "addr_standard_address"])
        elif any(keyword in question_lower for keyword in ("报警", "警情", "接警", "报警内容", "案发")):
            police_priority.extend(["police_alert", "alert_address_relation", "alert_caller"])
        elif any(keyword in question_lower for keyword in ("别名", "曾用名", "民间称呼", "地址别名")):
            police_priority.extend(["addr_alias", "addr_standard_address"])
        police_priority = [table_name for table_name in police_priority if table_name in available_police_tables]
        selected = list(dict.fromkeys(police_priority + police_selected + selected))[:max_tables]

    # 如果一个都没匹配到，就返回所有表（保底）
    if not selected:
        selected = [t["name"] for t in tables][:max_tables]
    
    logger.info(f"表选择结果: {selected} (原始表数: {len(tables)})")
    return selected

