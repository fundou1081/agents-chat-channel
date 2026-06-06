"""
Recipient 路由 / 别名解析.

LLM (尤其是小模型) 经常用 "dev" / "developer" / "team" / "前端" 这种
模糊 id 当 recipients. 我们需要把它们映射到真实 author id.

策略 (按优先级):
1. **精确匹配**: 已经是真实 author id → 保留
2. **Alias map**: "dev" → "zhang-frontend" 等
3. **模糊匹配**: 包含/前缀/display_name 匹配
4. **找不到**: log warning + drop
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..heartbeat import HeartbeatRegistry


# 别名表: 模糊 id → 真实 author id
# 默认映射到 zhang-frontend (因为 demo 里 zhang 是主力的 "干活" 角色)
RECIPIENT_ALIASES: dict[str, str] = {
    # 通用工程师
    "dev": "zhang-frontend",
    "developer": "zhang-frontend",
    "engineer": "zhang-frontend",
    "programmer": "zhang-frontend",
    "coder": "zhang-frontend",
    "team": "pm",
    "everyone": "pm",
    "all": "pm",
    # 前端
    "frontend": "zhang-frontend",
    "front-end": "zhang-frontend",
    "fe": "zhang-frontend",
    "ui": "zhang-frontend",
    "前端": "zhang-frontend",
    "前端工程师": "zhang-frontend",
    "小张": "zhang-frontend",
    "zhang": "zhang-frontend",
    # 后端
    "backend": "li-backend",
    "back-end": "li-backend",
    "be": "li-backend",
    "api": "li-backend",
    "后端": "li-backend",
    "后端工程师": "li-backend",
    "小李": "li-backend",
    "li": "li-backend",
    # PM
    "manager": "pm",
    "pm": "pm",
    "manager ": "pm",
    "经理": "pm",
    "林经理": "pm",
    # 上帝
    "god": "god",  # god 不在 author 列表, 但保留
}


def resolve_recipients(
    recipients: list[str],
    registry: "HeartbeatRegistry | None",
    persona_id: str = "",
) -> list[str]:
    """验证 + 重路由 recipients.

    Args:
        recipients: LLM 输出的 recipient list
        registry: HeartbeatRegistry (用来查 author 是否存在)
        persona_id: 当前 author id (用于 log)

    Returns:
        重路由后的 recipient list (去重 + 保留顺序)
    """
    if not registry:
        return list(recipients)

    result = []
    seen = set()
    for r in recipients:
        # 1. 精确匹配
        if registry.get(r):
            target = r
        else:
            # 2. alias 匹配
            lower = r.lower()
            target = None
            if lower in RECIPIENT_ALIASES:
                t = RECIPIENT_ALIASES[lower]
                if registry.get(t) or t == "god":
                    target = t
            # 3. 模糊匹配 (in / startswith)
            if not target:
                known = list(registry.authors.keys())
                for k in known:
                    if lower in k.lower() or k.lower() in lower:
                        target = k
                        break
                if not target:
                    for k in known:
                        a = registry.get(k)
                        if a and lower in a.persona.display_name.lower():
                            target = k
                            break
            # 4. drop
            if not target:
                print(f"  [{persona_id}] ⚠ unknown recipient: {r!r} (dropped)")
                continue
            if r != target:
                print(f"  [{persona_id}] ↪ {r!r} → {target!r}")

        # dedup based on resolved target
        if target in seen:
            continue
        seen.add(target)
        result.append(target)

    return result
