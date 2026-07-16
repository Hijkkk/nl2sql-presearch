import time

from fastapi import APIRouter,HTTPException

from backend.config.audit import log_audit
from backend.config.config import settings
from backend.adapters.registry import get_adapter
from backend.models.models import DataSourceInfo, MetadataResponse, ChatResponse, ChatRequest
from datetime import datetime
from loguru import logger

from backend.nl2sql.sql_generator import SQLGenerator
from backend.security.query_guard import QueryGuard

router = APIRouter(prefix="/api/v1", tags=["chat_module"])

sql_generator = SQLGenerator()

@router.get("/data-sources", response_model=list[DataSourceInfo])
async def list_data_sources():
    """列出支持的数据源"""
    return [
        DataSourceInfo(
            name="sqlite_demo",
            type="sqlite",
            status="connected",
            description="内置演示数据库（employees, departments, sales）- 适合测试复杂查询"
        )
    ]


@router.get("/metadata/{data_source}", response_model=MetadataResponse)
async def get_metadata(data_source: str):
    """获取数据源元数据"""
    adapter = get_adapter(data_source)
    try:
        meta = adapter.get_metadata()
        return MetadataResponse(
            data_source=data_source,
            tables=meta["tables"],
            total_tables=meta["total_tables"],
            generated_at=datetime.now()
        )
    except Exception as e:
        logger.error(f"Metadata error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    核心接口：自然语言 → SQL → 安全校验 → 执行 → 返回结果
    支持 Self-Correction + 审计日志
    """
    start_time = time.time()
    adapter = get_adapter(request.data_source)
    dialect = adapter.get_dialect()

    try:
        # 1. 获取元数据
        metadata = adapter.get_metadata()

        # 2. LLM 生成 SQL
        sql, thought, gen_error = await sql_generator.generate_sql(
            request.question, metadata, dialect
        )

        if gen_error:
            log_audit(
                question=request.question,
                generated_sql="",
                executed_sql="",
                data_source=request.data_source,
                row_count=0,
                status="failed",
                error_message=gen_error,
                execution_time=time.time() - start_time
            )
            return ChatResponse(
                success=False,
                question=request.question,
                error=f"SQL生成失败: {gen_error}",
                llm_thought=thought
            )

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
                execution_time=time.time() - start_time
            )
            return ChatResponse(
                success=False,
                question=request.question,
                sql=sql,
                error=f"安全拦截: {validation_error}",
                llm_thought=thought
            )

        # 4. 执行查询
        try:
            safe_sql = QueryGuard.sanitize_for_execution(
                sql, dialect, settings.max_rows_return
            )
            results, columns = adapter.execute_query(safe_sql)
            row_count = len(results)

            execution_time = time.time() - start_time

            # 写入审计日志
            log_audit(
                question=request.question,
                generated_sql=sql,
                executed_sql=safe_sql,
                data_source=request.data_source,
                row_count=row_count,
                status="success",
                execution_time=execution_time
            )

            insight = f"共返回 {row_count} 条记录。"

            return ChatResponse(
                success=True,
                question=request.question,
                sql=safe_sql,
                results=results,
                columns=columns,
                row_count=row_count,
                execution_time=round(execution_time, 2),
                llm_thought=thought,
                insight=insight
            )

        except Exception as exec_error:
            logger.error(f"Execution error: {exec_error}")

            # Self-Correction 尝试
            if settings.enable_self_correction:
                corrected_sql, correction_thought = await sql_generator.self_correct_sql(
                    sql, str(exec_error), request.question, metadata, dialect
                )
                if corrected_sql:
                    try:
                        is_safe2, _ = QueryGuard.validate_read_only(corrected_sql, dialect)
                        if is_safe2:
                            results2, cols2 = adapter.execute_query(corrected_sql)
                            exec_time = time.time() - start_time
                            log_audit(
                                question=request.question,
                                generated_sql=sql,
                                executed_sql=corrected_sql,
                                data_source=request.data_source,
                                row_count=len(results2),
                                status="success",
                                execution_time=exec_time
                            )
                            return ChatResponse(
                                success=True,
                                question=request.question,
                                sql=corrected_sql,
                                results=results2,
                                columns=cols2,
                                row_count=len(results2),
                                execution_time=round(exec_time, 2),
                                llm_thought=thought + "\n[自修复] " + correction_thought,
                                corrected_sql=corrected_sql,
                                insight="已自动修复SQL并成功执行"
                            )
                    except Exception:
                        pass

            log_audit(
                question=request.question,
                generated_sql=sql,
                executed_sql=sql,
                data_source=request.data_source,
                row_count=0,
                status="failed",
                error_message=str(exec_error),
                execution_time=time.time() - start_time
            )

            return ChatResponse(
                success=False,
                question=request.question,
                sql=sql,
                error=f"查询执行失败: {str(exec_error)}",
                llm_thought=thought
            )

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
            execution_time=time.time() - start_time
        )
        return ChatResponse(
            success=False,
            question=request.question,
            error=f"系统异常: {str(e)}"
        )


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "time": datetime.now().isoformat()
    }