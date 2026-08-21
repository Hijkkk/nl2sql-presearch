"""
受控代理路径的准备服务。

首次部署特意只包含记录：它验证了源路由和模式检索，

在任何模型生成的计划或 SQL 语句更改执行之前，验证了这些路由和模式检索。
"""
from __future__ import annotations

from backend.agent.contracts import MetadataContext, SourceCandidate
from backend.agent.tools import configured_source_descriptors, discover_sources, retrieve_metadata_context
from backend.config.config import settings


class AgentPreparationService:
    def prepare(
        self,
        question: str,
        source_hint: str | None = None,
    ) -> tuple[list[SourceCandidate], list[MetadataContext]]:
        """
        :param question: 用户自然语言问题
        :param source_hint:  可选的数据源提示（用户显式选择的数据源 ID）
        :return:
            list[SourceCandidate]: 经过筛选的候选数据源列表
            list[MetadataContext]: 对应的元数据上下文列表
        """
        # 获取已存在的数据源
        descriptors = configured_source_descriptors()
        hinted_descriptor = next((item for item in descriptors if item.source_id == source_hint), None)
        if hinted_descriptor:
            # 如果用户通过 source_hint 显式指定了数据源，
            # 则只在该数据源内进行检索（allowed_sources=[hinted_descriptor]）
            candidates = discover_sources(question, allowed_sources=[hinted_descriptor], limit=1)
        else:
            # 如果没有提示，则调用 discover_sources()
            # 自动发现候选源，数量受 settings.agent_source_candidate_limit 限制（默认 3 个）
            candidates = discover_sources(question, limit=settings.agent_source_candidate_limit)

        available_candidates: list[SourceCandidate] = []
        contexts: list[MetadataContext] = []

        for candidate in candidates:
            # 零分候选源：如果没有用户显式提示，得分 ≤ 0 的候选源被跳过
            # rationale：零分候选不是自动路由的证据，虽然对操作员可见，但不触发元数据 I/O
            if candidate.score <= 0 and not hinted_descriptor:
                continue
            try:
                # 检索元数据上下文
                # 对每个候选源调用 retrieve_metadata_context() 获取完整的模式上下文
                # 容错设计：如果某个数据源的元数据检索失败（例如达梦 JVM 未启动），静默跳过该源
                # 关键设计：数据源可用性是本地的——一个源的故障不应阻止其他源的问题到达规划和执行阶段
                context = retrieve_metadata_context(
                    question, candidate, object_limit=settings.agent_schema_object_limit
                )
            except Exception:
                continue
            # 只将成功检索到元数据的候选源及其上下文加入结果列表。
            available_candidates.append(candidate)
            contexts.append(context)
        return available_candidates, contexts
