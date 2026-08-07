import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from backend.crud import conversation as conversation_crud
from backend.config.audit import log_audit, prepare_result_sample
from backend.config.config import settings
from backend.adapters.registry import get_adapter
from backend.config.database import get_db
from backend.models.models import DataSourceInfo, MetadataResponse, ChatResponse, ChatRequest
from backend.models.user import User
from backend.routers.user import get_current_user
from datetime import datetime
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
    """Hide debug-only fields from API responses when configured."""
    if settings.nl2sql_debug_output:
        return response
    return response.model_copy(
        update={
            "sql": None,
            "llm_thought": None,
            "insight": None,
            "corrected_sql": None,
        }
    )


# 检测是否启用、连接测试
def append_configured_source(
    sources: list[DataSourceInfo],
    *,
    enabled: bool,
    name: str,
    source_type: str,
    description: str,
) -> None:
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
    """列出支持的数据源"""
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
    """列出当前可切换的大模型，和 config.py 中的真实配置保持一致。"""
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
    """获取数据源元数据"""
    adapter = get_adapter(data_source)
    try:
        meta = adapter.get_metadata()
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
    """Return adapter metadata cache status when supported."""
    adapter = get_adapter(data_source)
    return adapter.metadata_cache_status()


@router.post("/metadata/{data_source}/cache/warmup")
async def warmup_metadata_cache(data_source: str):
    """Warm up metadata cache for a data source."""
    adapter = get_adapter(data_source)
    started = time.perf_counter()
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
    """Clear and rebuild metadata cache for a data source."""
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
    """Run the record-only controlled graph without generating or executing SQL."""
    if not settings.agent_enabled:
        raise HTTPException(status_code=404, detail="受控 Agent 预检未启用")
    if not settings.agent_record_only:
        raise HTTPException(status_code=409, detail="当前仅支持 Agent 预检模式")

    state = await controlled_agent_graph.run(request.question)
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
    }


@router.post("/agent/run")
async def run_controlled_agent(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """Run the guarded single-source Agent path after record-only evaluation is disabled."""
    if not settings.agent_enabled:
        raise HTTPException(status_code=404, detail="受控 Agent 未启用")
    if settings.agent_record_only:
        raise HTTPException(status_code=409, detail="当前为仅记录模式；请先完成预检评估")

    state = await controlled_agent_graph.run(request.question)
    execution = state.get("execution")
    return {
        "request_id": state["request_id"],
        "status": state.get("status"),
        "record_only": False,
        "sql_executed": execution is not None,
        "execution": execution.model_dump() if execution else None,
        "answer": state.get("answer"),
        "plan": state["plan"].model_dump() if state.get("plan") else None,
        "validation": state["validation"].model_dump() if state.get("validation") else None,
        "review": state["reviewer_decision"].model_dump() if state.get("reviewer_decision") else None,
        "events": state.get("events", []),
        "error": state.get("error"),
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
    adapter = get_adapter(request.data_source)
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
        # 融合路径：用户授权的浏览器定位 -> 高德逆地理编码 -> 参数化 Gauss 查询。
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
