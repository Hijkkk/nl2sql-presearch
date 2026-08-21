import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from backend.crud import conversation as conversation_crud
from backend.config.audit import log_agent_trace, log_audit, prepare_result_sample, write_agent_execution_trace
from backend.config.config import settings
from backend.adapters.registry import get_adapter
from backend.config.database import get_db
from backend.models.models import DataSourceInfo, MetadataResponse, ChatResponse, ChatRequest
from backend.models.user import User
from backend.routers.user import get_current_user
from datetime import datetime
import time
import uuid
from loguru import logger

from backend.nl2sql.sql_generator import SQLGenerator
from backend.nl2sql.metadata_summarizer import MetadataSummarizer
from backend.api_services.amap_lbs_service import AmapLBSService
from backend.api_services.gauss_city_fusion_service import GaussCityFusionService
from backend.security.query_guard import QueryGuard
from backend.agent.graph import ControlledAgentGraph

router = APIRouter(prefix="/api/v1", tags=["chat_module"])

sql_generator = SQLGenerator()
metadata_summarizer = MetadataSummarizer()
amap_lbs_service = AmapLBSService()
gauss_city_fusion_service = GaussCityFusionService(amap_lbs_service)
controlled_agent_graph = ControlledAgentGraph()


def response_for_client(response: ChatResponse) -> ChatResponse:
    """
    控制是否向客户端暴露调试信息
    :param response:
    是一个 Pydantic 模型对象，包含完整查询结果，字段包括：
        sql - 生成的 SQL
        llm_thought - LLM 思考过程
        insight - 数据洞察/总结
        corrected_sql - 自修复后的 SQL
        results - 查询结果
        columns - 列名
        answer - 最终答案
        等等（见 ChatResponse）
    :return:
    """
    """Hide debug-only fields from API responses when configured."""
    if settings.nl2sql_debug_output:
        return response
    return response.model_copy(
        update={
            "sql": None,  # 隐藏 SQL
            "llm_thought": None,  # 隐藏 LLM 思考过程
            "insight": None,  # 隐藏数据洞察
            "corrected_sql": None,  # 隐藏自修复 SQL
        }
    )



def append_configured_source(
    sources: list[DataSourceInfo],
    *,
    enabled: bool,
    name: str,
    source_type: str,
    description: str,
) -> None:
    """
    检测是否启用，连接测试(ping)
    :param sources:
    :param enabled:
    :param name:
    :param source_type:
    :param description:
    :return:
    """
    if not enabled:
        return

    status = "connected"
    final_description = description
    try:
        adapter = get_adapter(name)
        if hasattr(adapter, "ping"):
            adapter.ping()
    except Exception as exc:
        status = "error"
        final_description = f"{description} 连接失败：{exc}"

    sources.append(
        DataSourceInfo(
            name=name,
            type=source_type,
            status=status,
            description=final_description,
        )
    )

@router.get("/data-sources", response_model=list[DataSourceInfo])
async def list_data_sources():
    """
    列出支持的数据源，前端调用返回给前端
    :return:[
        DataSourceInfo(
            name="sqlite_demo",
            type="sqlite",
            status="connected",
            description="内置演示数据库（employees, departments, sales）- 适合测试复杂查询"
        ),
        DataSourceInfo(
            name="mysql_prod",
            type="mysql",
            status="connected",  # 或 "error"
            description="MySQL生产数据库..."
        ),
        # ... 其他启用的数据源
    ]

    """
    sources = [
        DataSourceInfo(
            name="sqlite_demo",
            type="sqlite",
            status="connected",
            description="内置演示数据库（employees, departments, sales）- 适合测试复杂查询"
        )
    ]
    append_configured_source(
        sources,
        enabled=settings.mysql_query_enabled,
        name=settings.mysql_query_name,
        source_type="mysql",
        description=settings.mysql_query_description,
    )
    append_configured_source(
        sources,
        enabled=settings.postgres_query_enabled,
        name=settings.postgres_query_name,
        source_type="postgresql",
        description=settings.postgres_query_description,
    )
    append_configured_source(
        sources,
        enabled=settings.gauss_query_enabled,
        name=settings.gauss_query_name,
        source_type="gauss",
        description=settings.gauss_query_description,
    )
    append_configured_source(
        sources,
        enabled=settings.hive_query_enabled,
        name=settings.hive_query_name,
        source_type="hive",
        description=settings.hive_query_description,
    )
    append_configured_source(
        sources,
        enabled=settings.dameng_query_enabled,
        name=settings.dameng_query_name,
        source_type="dameng",
        description=settings.dameng_query_description,
    )
    append_configured_source(
        sources,
        enabled=settings.rest_api_enabled,
        name=settings.rest_api_name,
        source_type="rest_api",
        description=settings.rest_api_description,
    )
    append_configured_source(
        sources,
        enabled=settings.graphql_enabled,
        name=settings.graphql_name,
        source_type="graphql",
        description=settings.graphql_description,
    )

    return sources


@router.get("/models")
async def list_models():
    """
    列出当前可切换的大模型，以及相关信息，比如：模型ID、名字、描述、上下文、是否启用等信息
    :return:
    """
    return [
        {
            "id": SQLGenerator.DEFAULT_MODEL_ID,
            "name": settings.litellm_model,
            "provider": "公司内部 LiteLLM",
            "description": "默认 NL2SQL 代码生成模型",
            "max_context": 32768,
            "enabled": True,
        },
        {
            "id": SQLGenerator.XIYAN_OLLAMA_MODEL_ID,
            "name": settings.xiyan_ollama_model,
            "provider": "本地 Ollama",
            "description": "Ollama 量化版 XiYanSQL 3B，适合离线演示",
            "max_context": 32768,
            "enabled": settings.xiyan_ollama_enabled,
        },
        {
            "id": SQLGenerator.XIYAN_FINETUNE_MODEL_ID,
            "name": settings.xiyan_finetune_model,
            "provider": "本地 Ollama（LoRA Q4_K_M）",
            "description": "基于 XiYanSQL 3B LoRA 微调并量化的 NL2SQL 模型",
            "max_context": 32768,
            "enabled": settings.xiyan_finetune_enabled,
        },
        {
            "id": SQLGenerator.DASHSCOPE_MODEL_ID,
            "name": settings.dashscope_model,
            "provider": "阿里云 DashScope",
            "description": "备用通用推理模型",
            "max_context": 32768,
            "enabled": True,
        },
    ]


@router.get("/metadata/{data_source}", response_model=MetadataResponse)
async def get_metadata(
    data_source: str,
    summarize: bool = Query(default=False, description="是否返回摘要压缩后的元数据"),
):
    """
    获取数据源元数据
    :param data_source:
    :param summarize:
        /metadata/sqlite_demo?summarize=true 中的 summarize
            从 URL 中提取 summarize=true
            转换成 Python 的 bool 类型
            传给函数参数 summarize = True
    :return:
    """
    adapter = get_adapter(data_source)
    try:
        meta = adapter.get_metadata()
        #
        if summarize and settings.metadata_summary_enabled:
            meta = await metadata_summarizer.summarize_metadata(
                meta,
                data_source=data_source,
                # 这是给前端展示表结构用的接口，要求快速响应，不需要高质量摘要。
                use_llm=False,
            )
        return MetadataResponse(
            data_source=data_source,
            tables=meta["tables"],
            total_tables=meta["total_tables"],
            generated_at=datetime.now()
        )
    except Exception as e:
        logger.error(f"Metadata error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/metadata/{data_source}/cache/status")
async def metadata_cache_status(data_source: str):
    """
    返回元数据缓存状态
    :param data_source:
    :return:
    """
    adapter = get_adapter(data_source)
    return adapter.metadata_cache_status()


@router.post("/metadata/{data_source}/cache/warmup")
async def warmup_metadata_cache(data_source: str):
    """
    在系统空闲时提前加载数据库元数据到缓存，避免用户第一次请求时等待数据库查询。
    :param data_source:
    :return:
    """
    adapter = get_adapter(data_source)
    started = time.perf_counter()
    # 这会触发 get_metadata()，查询数据库并写入缓存
    metadata = adapter.warmup_metadata_cache()
    return {
        "data_source": data_source,
        "supported": adapter.metadata_cache_status().get("supported", False),
        "total_tables": metadata.get("total_tables", 0),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "cache": adapter.metadata_cache_status(),
    }


@router.post("/metadata/{data_source}/cache/refresh")
async def refresh_metadata_cache(data_source: str):
    """
    刷新缓存： 清除再创建
    :param data_source:
    :return:
    """
    adapter = get_adapter(data_source)
    cleared = adapter.clear_metadata_cache()
    started = time.perf_counter()
    metadata = adapter.warmup_metadata_cache()
    return {
        "data_source": data_source,
        "cleared": cleared,
        "total_tables": metadata.get("total_tables", 0),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "cache": adapter.metadata_cache_status(),
    }


# 这是一个后台预热接口，管理员在空闲时调用，提前用 LLM 生成高质量摘要并缓存。
# 之后用户聊天时直接读缓存，既享受高质量摘要，又不影响响应速度。
# # 管理员在部署后执行一次
# curl -X POST "http://127.0.0.1:8000/api/v1/metadata/sqlite_demo/summaries?use_llm=true"
# 表结构变了，强制重新生成 改了数据库字段、注释
# curl -X POST "http://127.0.0.1:8000/api/v1/metadata/sqlite_demo/summaries?use_llm=true&refresh=true"
@router.post("/metadata/{data_source}/summaries", response_model=MetadataResponse)
async def warmup_metadata_summaries(
    data_source: str,
    refresh: bool = Query(default=False, description="是否强制重新生成摘要"),
    use_llm: bool = Query(default=True, description="是否调用 LLM 生成业务摘要"),
):
    """预生成并缓存数据源元数据摘要，用于降低后续 NL2SQL Prompt 长度。"""
    adapter = get_adapter(data_source)
    try:
        meta = adapter.get_metadata()
        summarized_meta = await metadata_summarizer.summarize_metadata(
            meta,
            data_source=data_source,
            refresh=refresh,
            use_llm=use_llm,
        )
        return MetadataResponse(
            data_source=data_source,
            tables=summarized_meta["tables"],
            total_tables=summarized_meta["total_tables"],
            generated_at=datetime.now()
        )
    except Exception as e:
        logger.error(f"Metadata summary warmup error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agent/preview")
async def preview_controlled_agent(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """
    受控 Agent 的"预检模式" - 只运行规划流程， 用于测试
    不生成 SQL、不执行查询，返回 Agent 的思考过程和计划，供管理员/用户预览。
    :param request:
    :param current_user:
    :return:
    """

    # settings.agent_enabled = True - Agent 功能已启用
    # settings.agent_record_only = True - 当前处于仅记录模式
    # 如果任一不满足，直接报错：
    # 未启用 → 404
    # 不是预检模式 → 409
    if not settings.agent_enabled:
        raise HTTPException(status_code=404, detail="受控 Agent 预检未启用")
    if not settings.agent_record_only:
        raise HTTPException(status_code=409, detail="当前仅支持 Agent 预检模式")

    state = await controlled_agent_graph.run(request.question, source_hint=request.data_source)
    plan = state.get("plan")
    validation = state.get("validation")
    review = state.get("reviewer_decision")
    return {
        "request_id": state["request_id"],
        "status": state.get("status"),
        "record_only": True,
        "sql_executed": False,
        "source_candidates": [
            {"source_id": candidate.source_id, "score": candidate.score, "matched_terms": candidate.matched_terms}
            for candidate in state.get("candidates", [])
        ],
        "schema_contexts": [
            {
                "source_id": context.source.source_id,
                "schema_signature": context.schema_signature,
                "selected_object_ids": context.selected_object_ids,
                "schema_closure_object_ids": context.schema_closure_object_ids,
            }
            for context in state.get("contexts", [])
        ],
        "plan": plan.model_dump() if plan else None,
        "validation": validation.model_dump() if validation else None,
        "review": review.model_dump() if review else None,
        "events": state.get("events", []),
        "error": state.get("error"),
        "execution_time": state.get("execution_time", 0.0),
        "stage_timings": state.get("stage_timings", {}),
    }


@router.post("/agent/run")
async def run_controlled_agent(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    受控agent链路
    :param request:
    :param current_user:
    :param db:
    :return:
    """

    if not settings.agent_enabled:
        raise HTTPException(status_code=404, detail="受控 Agent 未启用")
    if settings.agent_record_only:
        raise HTTPException(status_code=409, detail="当前为仅记录模式；请先完成预检评估")

    async def persist_agent_exchange(
        *,
        data_source: str,
        model_id: str,
        content: str,
        execution: dict | None,
        execution_time: float,
        success: bool,
        error: str | None,
        presentation: dict,
    ) -> tuple[str | None, str | None]:
        """
        Keep Agent conversations in the same durable history as normal chat.
        :param data_source:
        :param model_id:
        :param content:
        :param execution:
        :param execution_time:
        :param success:
        :param error:
        :param presentation:
        :return:
        """
        execution = execution or {}
        try:
            return await conversation_crud.save_chat_exchange(
                db=db,
                user_id=current_user.id,
                conversation_id=request.conversation_id,
                question=request.question,
                data_source=data_source,
                model_id=model_id,
                model_config=request.model_conf,
                ai_content=content,
                sql=execution.get("sql"),
                columns=jsonable_encoder(execution.get("columns") or []),
                results=jsonable_encoder(execution.get("results") or []),
                row_count=execution.get("row_count") or 0,
                execution_time=execution_time,
                insight=content if success else None,
                success=success,
                error=error,
                presentation=presentation,
            )
        except Exception as history_error:
            logger.warning("Save Agent conversation history failed: {}", type(history_error).__name__)
            return None, None

    rest_adapter = get_adapter(settings.rest_api_name)
    # getattr(
    #     rest_adapter,                    # 要检查的对象
    #     "can_handle_controlled_question", # 要获取的属性名
    #     lambda _question: False          # 如果属性不存在，返回这个默认值
    # )
    # rest_adapter 有 can_handle_controlled_question 方法	返回该方法
    # rest_adapter 没有该方法	                                返回 lambda _question: False
    # rest_adapter 是 None	                                返回 lambda _question: False

    # 这是一个兜底函数，确保即使适配器没有这个方法，代码也不会报错

    # (...)(request.question)
    # 作用：调用上一步得到的函数
    if getattr(rest_adapter, "can_handle_controlled_question", lambda _question: False)(request.question):
        started = time.perf_counter()
        # 调用 REST 适配器的 execute_controlled_question()
        # 实际会转到 AmapLBSService.answer()
        response = rest_adapter.execute_controlled_question(request.question, request.client_location)
        # 记录执行耗时
        elapsed = round(time.perf_counter() - started, 3)
        # 生成唯一请求 ID，用于追踪整个 Agent 执行流程
        request_id = str(uuid.uuid4())
        execution = {
            "success": bool(response.success),
            "sql": response.sql,  # 实际是 "GET /v3/weather/weatherInfo"
            "columns": response.columns or [],  # 返回的列名
            "results": response.results or [],  # 返回的数据行
            "row_count": response.row_count or 0,  # 行数
            "retry_attempted": False,  # 没有重试机制
            "error": response.error,  # 错误信息
        }
        status = "executed_summarized" if response.success else "executed_failed"
        # 这是一个简化的计划，因为 REST API 调用是固定路径，不需要复杂的多表关联。
        plan = {
            "route_mode": "single_source",
            "subtasks": [{
                "id": "rest_api_adapter_lookup",
                "source_id": settings.rest_api_name,
                "operation_id": "rest_get",
                "goal": "使用受控 REST API 适配器回答地点问题",
                "object_ids": [],
                "output_fields": [],
                "depends_on": [],
            }],
            "merge_contract_id": None,
            "confidence": 1.0,   # 完全置信，因为是固定逻辑
        }
        # 直接通过验证和审查
        # 因为 REST API 调用是受控的，没有 SQL 注入风险，不需要复杂验证
        validation = {"status": "approved", "reason_codes": []}
        review = {"decision": "approve", "reason_codes": []}

        # retrieve - 检索数据源（直接找到 REST 适配器）
        # validate - 验证（直接通过）
        # execute - 执行（成功/失败）
        # summarize - 总结（成功则生成总结，失败则跳过）
        events = [
            {"node": "retrieve", "status": "ok", "source": settings.rest_api_name, "mode": "rest_api_adapter"},
            {"node": "validate", "status": "approved", "reason_codes": []},
            {"node": "execute", "status": "executed_success" if response.success else "executed_failed", "row_count": execution["row_count"], "error": response.error},
            {"node": "summarize", "status": "ok" if response.success else "skipped"},
        ]
        # 记录耗时
        timings = {"rest_api_adapter": elapsed, "total": elapsed}

        # 记录两套日志：
        # log_agent_trace - 通用审计日志
        # write_agent_execution_trace - Agent 执行追踪（持久化）
        log_agent_trace(
            request_id=request_id,
            question=request.question,
            status=status,
            events=events,
            route_mode="single_source",
            final_plan=plan,
            error_message=response.error,
            execution=execution,
            execution_time=elapsed,
            stage_timings=timings,
            model_id="system_rest_api_adapter",
            candidates=[{"source_id": settings.rest_api_name, "score": 1.0}],
            contexts=[],
        )
        write_agent_execution_trace(
            request_id=request_id,
            question=request.question,
            status=status,
            candidates=[{"source_id": settings.rest_api_name, "retrieval_method": "fixed_rest_api_adapter"}],
            contexts=[],
            plan=plan,
            validation=validation,
            review=review,
            execution={key: value for key, value in execution.items() if key != "results"},
            answer=response.answer or response.insight,
            events=events,
            error=response.error,
        )
        # response.answer - LLM 生成的答案（如果有）
        # response.insight - 自动生成的洞察
        # response.error - 错误信息
        # 兜底文本
        answer = response.answer or response.insight or response.error or "受控 REST 服务未返回摘要。"

        # 保存历史会话
        conversation_id, message_id = await persist_agent_exchange(
            data_source=settings.rest_api_name,
            model_id="system_rest_api_adapter",
            content=answer,
            execution=execution,
            execution_time=elapsed,
            success=bool(response.success),
            error=response.error,
            presentation={
                "is_agent": True,
                "status": status,
                "execution": {key: value for key, value in execution.items() if key not in {"results", "sql"}},
                "plan": plan,
                "validation": validation,
                "review": review,
                "events": events,
                "error": response.error,
                "execution_time": elapsed,
                "stage_timings": timings,
            },
        )
        return {
            "request_id": request_id,
            "status": status,
            "record_only": False,
            "sql_executed": False,
            "execution": execution,
            "answer": answer,
            "plan": plan,
            "validation": validation,
            "review": review,
            "events": events,
            "error": response.error,
            "execution_time": elapsed,
            "stage_timings": timings,
            "conversation_id": conversation_id,
            "message_id": message_id,
        }
    # request.model_fields_set - Pydantic 记录哪些字段是用户显式设置的
    # 如果前端传了 data_source → 使用它作为提示
    # 如果前端没传（使用默认值）→ None，让 Agent 自动选择
    source_hint = request.data_source if "data_source" in request.model_fields_set else None

    state = await controlled_agent_graph.run(request.question, source_hint=source_hint)

    execution = state.get("execution")
    execution_payload = execution.model_dump() if execution else None
    plan = state.get("plan")
    execution_time = state.get("execution_time", 0.0)
    succeeded = bool(execution and execution.success)
    answer = state.get("answer") or state.get("error") or (execution_payload or {}).get("error") or "受控 Agent 未完成本次查询。"
    conversation_id, message_id = await persist_agent_exchange(
        data_source=(plan.subtasks[0].source_id if plan and plan.subtasks else request.data_source),
        model_id=settings.agent_sql_model_id,
        content=answer,
        execution=execution_payload,
        execution_time=execution_time,
        success=succeeded,
        error=state.get("error") or (execution_payload or {}).get("error"),
        presentation={
            "is_agent": True,
            "status": state.get("status"),
            "execution": {key: value for key, value in (execution_payload or {}).items() if key not in {"results", "sql"}},
            "plan": plan.model_dump() if plan else None,
            "validation": state["validation"].model_dump() if state.get("validation") else None,
            "review": state["reviewer_decision"].model_dump() if state.get("reviewer_decision") else None,
            "events": state.get("events", []),
            "error": state.get("error"),
            "execution_time": execution_time,
            "stage_timings": state.get("stage_timings", {}),
        },
    )
    return {
        "request_id": state["request_id"],
        "status": state.get("status"),
        "record_only": False,
        "sql_executed": execution is not None,
        "execution": execution_payload,
        "answer": answer,
        "plan": plan.model_dump() if plan else None,
        "validation": state["validation"].model_dump() if state.get("validation") else None,
        "review": state["reviewer_decision"].model_dump() if state.get("reviewer_decision") else None,
        "events": state.get("events", []),
        "error": state.get("error"),
        "execution_time": execution_time,
        "stage_timings": state.get("stage_timings", {}),
        "conversation_id": conversation_id,
        "message_id": message_id,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    核心接口：自然语言 → SQL → 安全校验 → 执行 → 返回结果
    支持 Self-Correction + 审计日志
    """
    start_time = time.time()
    # 初始化一个对应数据源的适配器对象(数据库名字、ip、端口、用户名、密码等等)，并返回
    adapter = get_adapter(request.data_source)

    # 根据对应的适配器对象，调用内置函数，拿到他的数据库语言
    dialect = adapter.get_dialect()

    # 定义了一个内部函数 persist_response，作用是把每次对话保存到数据库（会话历史记录）
    async def persist_response(response: ChatResponse) -> ChatResponse:
        response.columns = jsonable_encoder(response.columns)
        response.results = jsonable_encoder(response.results)
        try:
            conversation_id, message_id = await conversation_crud.save_chat_exchange(
                db=db,
                user_id=current_user.id,
                conversation_id=request.conversation_id,
                question=request.question,
                data_source=request.data_source,
                model_id=request.model_id,
                model_config=request.model_conf,
                ai_content=response.answer or response.insight or response.llm_thought or response.error or "",
                sql=response.sql,
                columns=response.columns,
                results=response.results,
                row_count=response.row_count,
                execution_time=response.execution_time,
                insight=response.insight,
                success=response.success,
                error=response.error,
            )
            response.conversation_id = conversation_id
            response.message_id = message_id
        except Exception as history_error:
            logger.warning(f"Save conversation history failed: {history_error}")
        return response_for_client(response)

    try:
        # 融合路径：用户授权的浏览器定位 -> 数高德逆地理编码 -> 参化 Gauss 查询。
        # 不将定位文本交给模型拼接 SQL，也不影响普通 Gauss NL2SQL 查询。
        if gauss_city_fusion_service.can_handle(request.question, request.data_source):
            response = gauss_city_fusion_service.answer(
                request.question,
                request.client_location,
                adapter,
            )
            result_columns, result_sample, result_truncated = prepare_result_sample(
                response.columns or [], response.results or []
            )
            log_audit(
                question=request.question,
                generated_sql=response.sql or "",
                executed_sql=response.sql or "",
                data_source=request.data_source,
                row_count=response.row_count or 0,
                status="success" if response.success else "failed",
                error_message=response.error,
                execution_time=response.execution_time or 0,
                model_id=request.model_id,
                raw_model_output="[系统融合查询：未调用 SQL 生成模型]",
                llm_thought=response.llm_thought,
                stage_timings=response.stage_timings or {},
                query_guard_passed=True if response.success else None,
                result_columns=result_columns,
                result_sample=result_sample,
                result_truncated=result_truncated,
            )
            return await persist_response(response)

        # 高德地图LBS服务
        if (
            request.data_source == settings.rest_api_name  # 获取请求源 REST API NAME
            and settings.rest_api_service_mode == "amap_lbs"  # 判断是否为高德LBS服务
            and amap_lbs_service.can_handle(request.question)  # 查询是否有Key 以及用户的问题是否包含关键字
        ):
            # 调用高德LBS服务
            response = amap_lbs_service.answer(
                request.question,  # 问题
                client_location=request.client_location,  # 获取客户端位置信息
            )
            # 调用成功 让 LLM 把查询结果总结成一段自然语言回答，赋值给 response.answer
            if response.success:
                response.answer = await sql_generator.summarize_result(
                    request.question,  # 1. 用户的原始问题
                    response.columns or [],  # 2. 查询结果的列名列表（如果为 None 则用空列表）
                    response.results or [],  # 3. 查询结果的数据行（如果为 None 则用空列表）
                    answer_template=(request.model_conf or {}).get("answer_template", "brief"),  # 4. 回答模板风格
                    custom_instruction=(request.model_conf or {}).get("custom_instruction", ""),  # 5. 用户自定义指令
                    model_id=request.model_id,  # 6. 使用的 LLM 模型 ID
                    model_config=request.model_conf,  # 7. 模型的完整配置参数
                )
            # 准备用于审计日志的结果样本
            # 限制存储大小 - 不把所有结果都存进审计数据库
            # 收集列名 - 记录结果的字段结构
            # 标记是否截断 - 知道结果被裁剪了
            result_columns, result_sample, result_truncated = prepare_result_sample(
                response.columns or [],
                response.results or [],
            )
            log_audit(
                question=request.question,
                generated_sql=response.sql or "",
                executed_sql=response.sql or "",
                data_source=request.data_source,
                row_count=response.row_count or 0,
                status="success" if response.success else "failed",
                error_message=response.error,
                execution_time=response.execution_time or 0,
                model_id=request.model_id,
                stage_timings={"total": round(response.execution_time or 0, 3)},
                result_columns=result_columns,
                result_sample=result_sample,
                result_truncated=result_truncated,
            )
            return await persist_response(response)

        # 1. 获取元数据
        # 用户每次发消息都要等 SQL 生成结果，
        # 如果还要等 LLM 生成摘要，响应太慢。规则模式毫秒级完成，不影响用户体验。

        # 根据适配器获取数据源
        #     "tables": [
        #         {"name": "employees", "columns": [...], "primary_key": [...], "foreign_keys": [...]},
        #         {"name": "departments", "columns": [...], "primary_key": [...], "foreign_keys": [...]},
        #         {"name": "sales", "columns": [...], "primary_key": [...], "foreign_keys": [...]}
        #     ],
        #     "total_tables": 3
        metadata_started = time.perf_counter()
        metadata = adapter.get_metadata()

        # 数据源摘要是否开启
        if settings.metadata_summary_enabled:
            # 返回数据源的摘要版本
            metadata = await metadata_summarizer.summarize_metadata(
                metadata,
                data_source=request.data_source,
                use_llm=settings.metadata_summary_use_llm_in_chat,
            )
        metadata_seconds = time.perf_counter() - metadata_started

        # 2. LLM 生成 SQL
        generation_started = time.perf_counter()
        sql, thought, gen_error, generation_trace = await sql_generator.generate_sql(
            request.question,
            metadata,
            dialect,
            # model_id: "qwen3-coder-next-fp8"
            model_id=request.model_id,
            # model_config: {temperature: 0.2, top_p: 0.8, max_tokens: 2048, enable_sql_safety: true, return_insight: true,…}
            model_config=request.model_conf,
            data_source=request.data_source,
        )
        generation_seconds = time.perf_counter() - generation_started
        audit_context = {
            "rag_enabled": generation_trace["rag_enabled"],
            "rag_hits": generation_trace["rag_hits"],
            "selected_tables": generation_trace["selected_tables"],
            "prompt_token_estimate": generation_trace["prompt_token_estimate"],
            "stage_timings": {"metadata": round(metadata_seconds, 3), "sql_generation": round(generation_seconds, 3)},
            "model_id": request.model_id,
            "raw_model_output": generation_trace.get("raw_model_output", ""),
            "llm_thought": generation_trace.get("llm_thought", thought),
            "prompt_template": generation_trace.get("prompt_template", ""),
            "generation_cache_hit": bool(generation_trace.get("generation_cache_hit")),
        }
        # 2.1. SQL 生成报错 记录日志 返回
        if gen_error:
            log_audit(
                question=request.question,
                generated_sql="",
                executed_sql="",
                data_source=request.data_source,
                row_count=0,
                status="failed",
                error_message=gen_error,
                execution_time=time.time() - start_time,
                **{**audit_context, "stage_timings": {**audit_context["stage_timings"], "total": round(time.time() - start_time, 3)}},
            )
            return await persist_response(ChatResponse(
                success=False,
                question=request.question,
                error=f"SQL生成失败: {gen_error}",
                execution_time=round(time.time() - start_time, 3),
                llm_thought=thought,
                stage_timings={**audit_context["stage_timings"], "total": round(time.time() - start_time, 3)},
            ))

        # 3. 安全验证（核心！）
        is_safe, validation_error = QueryGuard.validate_read_only(sql, dialect)
        if not is_safe:
            logger.warning(f"安全拦截: {validation_error}")
            log_audit(
                question=request.question,
                generated_sql=sql,
                executed_sql="",
                data_source=request.data_source,
                row_count=0,
                status="blocked",
                error_message=validation_error,
                execution_time=time.time() - start_time,
                query_guard_passed=False,
                **{**audit_context, "stage_timings": {**audit_context["stage_timings"], "total": round(time.time() - start_time, 3)}},
            )
            return await persist_response(ChatResponse(
                success=False,
                question=request.question,
                sql=sql,
                error=f"安全拦截: {validation_error}",
                execution_time=round(time.time() - start_time, 3),
                llm_thought=thought,
                stage_timings={**audit_context["stage_timings"], "total": round(time.time() - start_time, 3)},
            ))

        # 4. 执行查询
        try:
            safe_sql = QueryGuard.sanitize_for_execution(
                sql, dialect, settings.max_rows_return
            )
            database_started = time.perf_counter()
            results, columns = adapter.execute_query(safe_sql)
            database_seconds = time.perf_counter() - database_started
            row_count = len(results)

            execution_time = time.time() - start_time

            insight = f"共返回 {row_count} 条记录。"
            # 根据查询结果整理答案
            summary_started = time.perf_counter()
            answer = await sql_generator.summarize_result(
                request.question,
                columns,
                results,
                answer_template=(request.model_conf or {}).get("answer_template", "brief"),
                custom_instruction=(request.model_conf or {}).get("custom_instruction", ""),
                model_id=request.model_id,
                model_config=request.model_conf,
            )
            summary_seconds = time.perf_counter() - summary_started
            stage_timings = {
                **audit_context["stage_timings"],
                "database": round(database_seconds, 3),
                "result_summary": round(summary_seconds, 3),
                "total": round(time.time() - start_time, 3),
            }
            result_columns, result_sample, result_truncated = prepare_result_sample(columns, results)
            log_audit(
                question=request.question,
                generated_sql=sql,
                executed_sql=safe_sql,
                data_source=request.data_source,
                row_count=row_count,
                status="success",
                execution_time=time.time() - start_time,
                query_guard_passed=True,
                result_columns=result_columns,
                result_sample=result_sample,
                result_truncated=result_truncated,
                **{**audit_context, "stage_timings": stage_timings},
            )

            return await persist_response(ChatResponse(
                success=True,
                question=request.question,
                sql=safe_sql,
                results=results,
                columns=columns,
                row_count=row_count,
                execution_time=round(time.time() - start_time, 3),
                llm_thought=thought,
                insight=insight,
                answer=answer,
                stage_timings=stage_timings,
            ))

        # 如果执行失败，尝试自修复
        except Exception as exec_error:
            logger.error(f"Execution error: {exec_error}")
            database_seconds = locals().get("database_seconds", 0.0)
            correction_attempted = False
            corrected_sql_for_audit = None

            # Self-Correction 尝试
            if settings.enable_self_correction:
                correction_attempted = True
                corrected_sql, correction_thought = await sql_generator.self_correct_sql(
                    sql,
                    str(exec_error),
                    request.question,
                    metadata,
                    dialect,
                    model_id=request.model_id,
                    model_config=request.model_conf,
                    data_source=request.data_source,
                )
                corrected_sql_for_audit = corrected_sql or None
                if corrected_sql:
                    try:
                        is_safe2, _ = QueryGuard.validate_read_only(corrected_sql, dialect)
                        if is_safe2:
                            correction_database_started = time.perf_counter()
                            results2, cols2 = adapter.execute_query(corrected_sql)
                            correction_database_seconds = time.perf_counter() - correction_database_started
                            exec_time = time.time() - start_time
                            summary_started = time.perf_counter()
                            answer2 = await sql_generator.summarize_result(
                                request.question,
                                cols2,
                                results2,
                                answer_template=(request.model_conf or {}).get("answer_template", "brief"),
                                custom_instruction=(request.model_conf or {}).get("custom_instruction", ""),
                                model_id=request.model_id,
                                model_config=request.model_conf,
                            )
                            summary_seconds = time.perf_counter() - summary_started
                            stage_timings = {
                                **audit_context["stage_timings"],
                                "database": round(database_seconds + correction_database_seconds, 3),
                                "result_summary": round(summary_seconds, 3),
                                "total": round(exec_time, 3),
                            }
                            result_columns, result_sample, result_truncated = prepare_result_sample(cols2, results2)
                            log_audit(
                                question=request.question,
                                generated_sql=sql,
                                executed_sql=corrected_sql,
                                data_source=request.data_source,
                                row_count=len(results2),
                                status="success",
                                execution_time=exec_time,
                                query_guard_passed=True,
                                correction_attempted=True,
                                corrected_sql=corrected_sql,
                                result_columns=result_columns,
                                result_sample=result_sample,
                                result_truncated=result_truncated,
                                **{**audit_context, "llm_thought": (thought or "") + "\n[自修复] " + correction_thought, "stage_timings": stage_timings},
                            )
                            return await persist_response(ChatResponse(
                                success=True,
                                question=request.question,
                                sql=corrected_sql,
                                results=results2,
                                columns=cols2,
                                row_count=len(results2),
                                execution_time=round(exec_time, 3),
                                llm_thought=thought + "\n[自修复] " + correction_thought,
                                corrected_sql=corrected_sql,
                                answer=answer2,
                                insight=answer2 or "已自动修复 SQL 并成功执行。",
                                stage_timings=stage_timings,
                            ))
                    except Exception:
                        pass

            stage_timings = {
                **audit_context["stage_timings"],
                "database": round(database_seconds, 3),
                "result_summary": 0.0,
                "total": round(time.time() - start_time, 3),
            }
            log_audit(
                question=request.question,
                generated_sql=sql,
                executed_sql=sql,
                data_source=request.data_source,
                row_count=0,
                status="failed",
                error_message=str(exec_error),
                execution_time=time.time() - start_time,
                query_guard_passed=True,
                correction_attempted=correction_attempted,
                corrected_sql=corrected_sql_for_audit,
                **{**audit_context, "stage_timings": stage_timings},
            )

            return await persist_response(ChatResponse(
                success=False,
                question=request.question,
                sql=sql,
                error=f"查询执行失败: {str(exec_error)}",
                llm_thought=thought,
                execution_time=round(time.time() - start_time, 3),
                stage_timings=stage_timings,
            ))

    # 聊天端错误--获取不到数据源、连不上高德数据源、sql修复失败
    except Exception as e:
        logger.exception("Chat endpoint unexpected error")
        log_audit(
            question=request.question,
            generated_sql="",
            executed_sql="",
            data_source=request.data_source,
            row_count=0,
            status="failed",
            error_message=str(e),
            execution_time=time.time() - start_time,
            model_id=request.model_id,
            stage_timings={"total": round(time.time() - start_time, 3)},
        )
        return await persist_response(ChatResponse(
            success=False,
            question=request.question,
            error=f"系统异常: {str(e)}",
            execution_time=round(time.time() - start_time, 3),
            stage_timings={"total": round(time.time() - start_time, 3)},
        ))


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "time": datetime.now().isoformat()
    }
