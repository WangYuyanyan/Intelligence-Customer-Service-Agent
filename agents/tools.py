"""Agent 工具定义与实现。

所有 Agent 工具集中在这里，编排器只负责：
  1. 根据 Agent 类型暴露工具白名单
  2. 执行 LLM 返回的 tool_use
  3. 将工具结果回传给 LLM

工具本身保持确定性、可测试，并明确区分：
  - 当前请求分析
  - 技术排障建议
  - 账单字段核验
  - 人工升级摘要
  - 共享知识库 RAG

订单查询、退款执行、账单修改等需要真实业务系统授权的动作不在这里伪造。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from agents.agent_orchestrator import Request


AgentToolHandler = Callable[["Request", Dict[str, Any]], Union[Any, Awaitable[Any]]]


@dataclass(frozen=True)
class AgentToolSpec:
    """Agent 可见工具的定义和执行函数。"""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler


def make_tool(
    name: str,
    description: str,
    properties: Dict[str, Any],
    handler: AgentToolHandler,
    required: Optional[List[str]] = None,
) -> AgentToolSpec:
    """创建带 JSON Schema 的 Agent 工具。"""
    return AgentToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        handler=handler,
    )


def inspect_request_context(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """通用客服工具：返回脱敏后的当前请求快照。"""
    return {
        "intent": req.intent.value if req.intent else None,
        "intent_group": req.intent_group,
        "urgency": req.urgency.name if req.urgency else None,
        "intent_confidence": round(req.intent_confidence, 4),
        "entities": req.entities or {},
        "context_available": bool(req.context),
        "requested_focus": str(args.get("focus", "general"))[:40],
    }


def suggest_required_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """通用客服工具：按业务类型计算下一轮只需询问的字段。"""
    intent = req.intent.value if req.intent else "other"
    fields: List[str] = []
    if intent in {"order_status", "logistics"}:
        fields = ["Order number or order placement time"]  # 中文原文：fields = ["订单号或下单时间"]
    elif intent in {"account", "account_security"}:
        fields = ["Login method or account identifier", "Time of occurrence"]  # 中文原文：fields = ["登录方式或账号标识", "问题发生时间"]
    elif intent in {"complaint", "request"}:
        fields = ["Time of the incident", "Desired resolution"]  # 中文原文：fields = ["事件时间", "期望处理方式"]
    elif intent == "other":
        fields = ["The specific issue you need help resolving"]  # 中文原文：fields = ["希望解决的具体问题"]
    return {
        "intent": intent,
        "required_fields": fields,
        "known_entities": req.entities or {},
    }


def lookup_error_code(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """技术工具：解释常见错误码的排查方向，不声称读取了服务端日志。"""
    code = str(args.get("error_code", "")).upper().strip()
    mapping = {
        "401": ("Authentication failed", ["Check whether the token or API key has expired", "Verify the request timestamp and signature", "Check the account login status"]),  # 中文原文："401": ("认证失败", ["确认 Token/API Key 是否过期", "确认请求时间戳和签名", "确认账号登录状态"]),
        "403": ("Insufficient permissions", ["Check account permissions or plan entitlements", "Check resource permissions and the IP allowlist"]),  # 中文原文："403": ("权限不足", ["确认账号或套餐权限", "确认资源权限和 IP 白名单"]),
        "404": ("Resource or path not found", ["Verify the endpoint path and environment", "Verify the resource identifier"]),  # 中文原文："404": ("资源或路径不存在", ["确认接口路径和环境", "确认资源标识是否正确"]),
        "500": ("Server-side processing error", ["Record the request_id and time of occurrence", "Check dependent services, parameter formats, and server-side logs"]),  # 中文原文："500": ("服务端处理异常", ["记录 request_id 和发生时间", "检查依赖服务、参数格式和服务端日志"]),
    }
    meaning, steps = mapping.get(
        code,
        ("Unrecognized error code", ["Provide the full error message, time of occurrence, and runtime environment"]),  # 中文原文：("暂未识别的错误码", ["补充完整错误信息、发生时间和运行环境"]),
    )
    return {
        "error_code": code,
        "meaning": meaning,
        "next_steps": steps,
        "server_log_checked": False,
    }


def build_diagnostic_plan(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """技术工具：生成低风险排障顺序。"""
    environment = str(args.get("environment", "unknown"))[:80]
    reproduced = bool(args.get("reproduced", False))
    steps = [
        "Reproduce the issue and record the full error message",  # 中文原文："复现并记录完整错误信息",
        "Check the network, DNS, proxy, and certificates",  # 中文原文："确认网络、DNS、代理和证书",
        "Check versions, configuration, and permissions",  # 中文原文："确认版本、配置和权限",
    ]
    if reproduced:
        steps.append("Reproduce with a minimal request and record the request_id")  # 中文原文：steps.append("用最小请求复现并记录 request_id")
    return {
        "environment": environment,
        "reproduced": reproduced,
        "diagnostic_steps": steps,
    }


def check_billing_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """账单工具：检查必要核验字段是否齐全。"""
    fields = {
        "order_id": bool(req.entities.get("order_id")),
        "amount": bool(req.entities.get("amount")),
        "date": bool(req.entities.get("date")),
        "payment_channel": bool(args.get("payment_channel")),
    }
    return {
        "fields": fields,
        "missing_fields": [name for name, present in fields.items() if not present],
        "can_confirm_refund": False,
        "reason": "This tool only checks fields; it does not connect to order or payment systems",  # 中文原文："reason": "当前工具只做字段检查，不连接订单或支付系统",
    }


def compare_amounts(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """账单工具：只做用户明确提供金额之间的算术。"""
    try:
        # bool 是 int 的子类，需显式拒绝；非有限值不能进入 JSON 工具结果。
        values = (args["amount_a"], args["amount_b"])
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ValueError
        first, second = (float(value) for value in values)
        if not all(math.isfinite(value) for value in (first, second)):
            raise ValueError
        # 使用十进制计算金额差值，避免二进制浮点导致分位舍入错误。
        difference = float((Decimal(str(values[0])) - Decimal(str(values[1]))).quantize(Decimal("0.01")))
        if not math.isfinite(difference):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError, DecimalException):
        return {"success": False, "error": "amount_a and amount_b must be finite numbers within the supported range"}  # amount_a 和 amount_b 必须是有效范围内的有限数字
    return {
        "success": True,
        "amount_a": first,
        "amount_b": second,
        "difference": difference,
        "interpretation": "This is only the difference between the amounts; it does not establish duplicate charges or refund eligibility",  # 中文原文："interpretation": "仅表示金额差值，不代表重复扣款或退款结论",
    }


def create_handoff_summary(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """升级工具：生成可交给人工客服的结构化摘要。"""
    return {
        "request_id": req.request_id,
        "reason": str(args.get("reason", "Further verification by human support is required"))[:120],  # 中文原文："reason": str(args.get("reason", "需要人工客服继续核验"))[:120],
        "intent": req.intent.value if req.intent else "unknown",
        "urgency": req.urgency.name if req.urgency else "UNKNOWN",
        "entities": req.entities or {},
        "sensitive_data_required": False,
    }


def build_shared_rag_tools(tool_manager: Any) -> Dict[str, AgentToolSpec]:
    """构建所有 Agent 可共享的 RAG 工具。"""

    async def search_knowledge_base(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or req.message or "").strip()
        top_k = args.get("top_k", 5)
        # 与 JSON Schema 保持一致；不能把小数截断，或把 0 静默改成默认值。
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            return {"success": False, "error": "top_k must be a positive integer", "results": []}  # top_k 必须是正整数
        if not query:
            return {"success": False, "error": "query must not be empty", "results": []}  # 中文原文：return {"success": False, "error": "query 不能为空", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "The RAG tool has not been initialized", "results": []}  # 中文原文：return {"success": False, "error": "RAG 工具未初始化", "results": []}

        result = await tool_manager.search_with_rewrite(
            "knowledge_search",
            query,
            top_k=top_k,
        )
        if not getattr(result, "success", False):
            return {
                "success": False,
                "query": query,
                "error": getattr(result, "error", None) or "Knowledge base search failed",  # 知识库检索失败；空错误信息也使用默认提示
                "results": [],
                "reranked": False,
            }

        return {
            "success": True,
            "query": query,
            "top_k": top_k,
            "results": result.data,
            "reranked": bool(getattr(result, "reranked", False)),
        }

    return {
        "search_knowledge_base": make_tool(
            "search_knowledge_base",
            "Search the knowledge base and return the most relevant document excerpts for general, technical, billing, and escalation support.",  # 中文原文："检索知识库并返回最相关的文档片段；可用于通用、技术、账单和升级场景。",
            {
                "query": {"type": "string", "description": "User question or search keywords"},  # 中文原文："query": {"type": "string", "description": "用户问题或检索关键词"},
                "top_k": {"type": "integer", "minimum": 1, "description": "Number of results to return"},  # 返回结果条数，至少为 1
            },
            search_knowledge_base,
            required=["query"],
        )
    }


def general_tools() -> Dict[str, AgentToolSpec]:
    return {
        "inspect_request_context": make_tool(
            "inspect_request_context",
            "Inspect the current request's intent, urgency, entities, and context availability without querying external business systems.",  # 中文原文："查看当前请求的意图、紧急度、实体和上下文可用性；不查询外部业务系统。",
            {"focus": {"type": "string", "description": "Business area to focus on"}},  # 中文原文：{"focus": {"type": "string", "description": "希望关注的业务方向"}},
            inspect_request_context,
        ),
        "suggest_required_fields": make_tool(
            "suggest_required_fields",
            "Suggest the fields to request from the user next based on the current intent.",  # 中文原文："根据当前意图建议下一轮只需向用户补充的字段。",
            {},
            suggest_required_fields,
        ),
    }


def technical_tools() -> Dict[str, AgentToolSpec]:
    return {
        "lookup_error_code": make_tool(
            "lookup_error_code",
            "Explain possible meanings of common HTTP error codes and low-risk troubleshooting steps without reading server-side logs.",  # 中文原文："解释常见 HTTP 错误码的可能含义和低风险排查方向；不会读取服务端日志。",
            {"error_code": {"type": "string", "description": "For example, 401, 403, or 500"}},  # 中文原文：{"error_code": {"type": "string", "description": "例如 401、403、500"}},
            lookup_error_code,
            required=["error_code"],
        ),
        "build_diagnostic_plan": make_tool(
            "build_diagnostic_plan",
            "Build a troubleshooting sequence based on the environment and reproducibility without changing configuration or performing other operations.",  # 中文原文："根据运行环境和是否可复现生成排障顺序，不执行修改配置等操作。",
            {
                "environment": {"type": "string", "description": "For example, app, browser, server, or Docker"},  # 中文原文："environment": {"type": "string", "description": "App、浏览器、服务端或 Docker 等"},
                "reproduced": {"type": "boolean", "description": "Whether the issue can be consistently reproduced"},  # 中文原文："reproduced": {"type": "boolean", "description": "问题是否可以稳定复现"},
            },
            build_diagnostic_plan,
            required=["environment", "reproduced"],
        ),
    }


def billing_tools() -> Dict[str, AgentToolSpec]:
    return {
        "check_billing_fields": make_tool(
            "check_billing_fields",
            "Check whether billing verification fields are complete without connecting to order, payment, or refund systems.",  # 中文原文："检查账单核验字段是否齐全；不连接订单、支付或退款系统。",
            {"payment_channel": {"type": "string", "description": "Payment method, such as WeChat Pay, Alipay, or bank card"}},  # 中文原文：{"payment_channel": {"type": "string", "description": "支付渠道，例如微信、支付宝、银行卡"}},
            check_billing_fields,
        ),
        "compare_amounts": make_tool(
            "compare_amounts",
            "Calculate the difference between two amounts explicitly provided by the user without determining duplicate charges or issuing refunds.",  # 中文原文："计算用户明确提供的两笔金额差值；不判断是否重复扣款，也不执行退款。",
            {
                "amount_a": {"type": "number", "description": "First amount"},  # 中文原文："amount_a": {"type": "number", "description": "第一笔金额"},
                "amount_b": {"type": "number", "description": "Second amount"},  # 中文原文："amount_b": {"type": "number", "description": "第二笔金额"},
            },
            compare_amounts,
            required=["amount_a", "amount_b"],
        ),
    }


def escalation_tools() -> Dict[str, AgentToolSpec]:
    return {
        "create_handoff_summary": make_tool(
            "create_handoff_summary",
            "Generate a structured handoff summary for human support without creating an actual support ticket.",  # 中文原文："生成交给人工客服的结构化交接摘要，不会创建真实工单。",
            {"reason": {"type": "string", "description": "Reason for escalation"}},  # 中文原文：{"reason": {"type": "string", "description": "需要升级的原因"}},
            create_handoff_summary,
        ),
    }
