"""Neutral mock LLM provider used when no runtime API key is configured.

The provider is intentionally schema-oriented: it returns valid, minimal JSON for
known generation intents, but it must not invent a reusable plot, genre, or fixed
story trope. This keeps no-key/local-dev paths from polluting production data with
hidden fallback narratives.
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Callable, Dict

from domain.ai.services.llm_service import GenerationConfig, GenerationResult, LLMService
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage


JsonObject = Dict[str, Any]


class MockResponseFactory:
    """Build contract-shaped, genre-neutral mock responses.

    The factory deliberately describes structure rather than story content. If a
    caller needs real creative material, it must use a configured LLM provider.
    """

    def build(self, prompt: Prompt) -> str:
        intent = self._detect_intent(prompt)
        if intent == "outline_cohort":
            return self._outline_cohort()
        if intent == "act_plan":
            return self._act_plan(prompt)
        if intent == "chapter_preplan":
            return self._chapter_preplan()
        if intent == "prose":
            return self._prose(prompt)
        if intent == "narrative_sync":
            return self._narrative_sync()
        if intent == "memory_extraction":
            return self._memory_extraction(prompt)
        builders: Dict[str, Callable[[], str]] = {
            "macro_refactor": self._macro_refactor,
            "macro_plan": self._macro_plan,
            "worldbuilding": self._worldbuilding,
            "characters": self._characters,
            "locations": self._locations,
            "main_plot_options": self._main_plot_options,
            "plot_outline": self._plot_outline,
            "chapter_review": self._chapter_review,
            "style": self._style,
        }
        return builders.get(intent, self._default)()

    def _detect_intent(self, prompt: Prompt) -> str:
        text = f"{prompt.system}\n{prompt.user}".lower()

        if (
            "narrative_text" in text
            and "creative_goal" in text
            and "entry_state" in text
            and "exit_state" in text
            and "conflicts" in text
            and "state_changes" in text
            and "handoff_conditions" in text
            and "chapter_start" in text
            and "chapter_end" in text
        ):
            return "outline_cohort"
        if (
            "natural_language_suggestion" in text
            and "suggested_mutations" in text
            and "suggested_tags" in text
            and "reasoning" in text
        ):
            return "macro_refactor"
        if "setup_main_plot_options_v1" in text or "plot_options" in text or "主线候选" in text:
            return "main_plot_options"
        if '"plot_outline"' in text or "剧情总纲" in text or "setup.plot_outline" in text:
            return "plot_outline"
        if "宏观结构" in text or "结构框架" in text or "部-卷-幕" in text or '"parts"' in text:
            return "macro_plan"
        if (
            '"detail_title"' in text
            and '"key_plot_points"' in text
            and '"chapter_plan"' in text
        ):
            return "chapter_preplan"
        if "文章字数" in text and "请生成正文内容" in text:
            return "prose"
        if (
            '"summary"' in text
            and '"key_events"' in text
            and '"open_threads"' in text
            and '"relation_triples"' in text
        ):
            return "narrative_sync"
        if all(
            key in text
            for key in ("completed_beats", "revealed_clues", "fact_violations")
        ):
            return "memory_extraction"
        if "请为这一幕规划" in text and '"chapters"' in text:
            return "act_plan"
        # Stage prompts include worldbuilding as context. Their explicit output
        # schema must win over that shared context when no provider is configured.
        if '"characters"' in text:
            return "characters"
        if '"locations"' in text:
            return "locations"
        if "worldbuilding" in text or "世界观" in text or "核心法则" in text:
            return "worldbuilding"
        if "characters" in text or "人物" in text or "角色" in text:
            return "characters"
        if "locations" in text or "地点" in text or "地图" in text:
            return "locations"
        if "章节 ai 审阅" in text or "严格但务实的小说责任编辑" in text or '"score"' in text and '"issues"' in text:
            return "chapter_review"
        if "文风公约" in text or "style convention" in text or "style" in text:
            return "style"
        return "default"

    def _json(self, payload: JsonObject) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _macro_plan(self) -> str:
        return self._json(
            {
                "parts": [
                    {
                        "number": 1,
                        "title": "第一部：目标建立",
                        "description": "围绕用户设定建立核心目标、主要阻力与阶段性代价。",
                        "suggested_chapter_count": 3,
                        "themes": ["目标", "阻力", "选择"],
                        "volumes": [
                            {
                                "number": 1,
                                "title": "第一卷：起始压力",
                                "description": "让关键人物在明确压力下做出第一轮选择。",
                                "suggested_chapter_count": 3,
                                "acts": [
                                    {
                                        "number": 1,
                                        "title": "第一幕：问题出现",
                                        "description": "呈现用户设定中的核心问题与即时后果。",
                                        "suggested_chapter_count": 1,
                                        "key_events": ["核心问题显性化", "人物目标被迫明确"],
                                        "narrative_arc": "从稳定状态进入需要行动的局面。",
                                        "conflicts": ["个人目标与外部压力"],
                                        "plot_points": ["建立起点", "触发选择"],
                                        "key_characters": [],
                                        "key_locations": [],
                                    },
                                    {
                                        "number": 2,
                                        "title": "第二幕：代价确认",
                                        "description": "通过一次受阻确认目标并非轻易可得。",
                                        "suggested_chapter_count": 1,
                                        "key_events": ["第一次尝试受阻", "代价被具体化"],
                                        "narrative_arc": "行动带来代价，人物开始调整策略。",
                                        "conflicts": ["短期收益与长期风险"],
                                        "plot_points": ["尝试", "受阻"],
                                        "key_characters": [],
                                        "key_locations": [],
                                    },
                                    {
                                        "number": 3,
                                        "title": "第三幕：方向锁定",
                                        "description": "用一个不可逆选择锁定后续推进方向。",
                                        "suggested_chapter_count": 1,
                                        "key_events": ["关键选择发生", "阶段目标升级"],
                                        "narrative_arc": "从被动应对转为主动推进。",
                                        "conflicts": ["安全退路与主动承担"],
                                        "plot_points": ["选择", "升级"],
                                        "key_characters": [],
                                        "key_locations": [],
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        )

    def _worldbuilding(self) -> str:
        return self._json(
            {
                "style": self._style_text(),
                "worldbuilding": {
                    "core_rules": {
                        "power_system": "依据用户设定建立核心能力、资源或规则体系，明确获得门槛、使用边界与失败代价。",
                        "physics_rules": "世界运行遵循用户设定的基础逻辑，特殊规则必须前后一致并能影响人物选择。",
                        "magic_tech": "关键工具、能力或技术只服务冲突推进，不替人物自动解决核心问题。",
                    },
                    "geography": {
                        "terrain": "地点层级围绕行动路线、信息差与冲突压力组织，避免只做背景陈列。",
                        "climate": "环境条件应能影响行动难度、节奏变化或人物判断。",
                        "resources": "关键资源按照稀缺性、获取成本和使用风险分布。",
                        "ecology": "人与环境的互动形成可复用约束，并在重要场景中产生后果。",
                    },
                    "society": {
                        "politics": "组织规则与权力关系应解释谁能决策、谁承担代价、谁会阻止改变。",
                        "economy": "交换关系围绕资源、机会与风险展开，推动人物做取舍。",
                        "class_system": "身份差异必须转化为行动权限、信息可得性或冲突压力。",
                    },
                    "culture": {
                        "history": "过去事件为当下冲突提供成因，但不替代当前行动。",
                        "religion": "信念、传统或公共叙事应影响人物判断与群体反应。",
                        "taboos": "禁忌用于制造边界和代价，触碰后必须产生可见后果。",
                    },
                    "daily_life": {
                        "food_clothing": "日常细节体现身份、资源状况和压力，不做无效铺陈。",
                        "language_slang": "语言风格服务角色区分、阵营差异和场景真实感。",
                        "entertainment": "休闲与传播方式可承载舆论、关系变化或信息流动。",
                    },
                },
            }
        )

    def _characters(self) -> str:
        return self._json(
            {
                "characters": [
                    {
                        "name": "核心人物甲",
                        "gender": "未指定",
                        "age": "未指定",
                        "role": "主角",
                        "description": "围绕用户设定承担主要目标的人物，必须通过选择推动剧情。",
                        "appearance": "",
                        "personality": "遇事先压住情绪，再处理问题。",
                        "background": "曾在高压环境下独自承担错误后果。",
                        "public_profile": "外界可见身份由用户设定决定，当前仅保留结构占位。",
                        "hidden_profile": "",
                        "reveal_chapter": None,
                        "mental_state": "承压",
                        "mental_state_reason": "核心问题出现后需要在有限信息下行动。",
                        "core_belief": "行动必须承担后果。",
                        "moral_taboos": ["不把无关者当作代价", "不伪造关键事实"],
                        "core_motivation": "解决当前核心问题。",
                        "inner_lack": "学会在代价明确时仍然做出有效选择。",
                        "ghost": "曾因判断不足付出代价。",
                        "want": "解决当前核心问题。",
                        "need": "学会在代价明确时仍然做出有效选择。",
                        "flaw": "容易把问题独自扛下。",
                        "verbal_tic": "",
                        "idle_behavior": "压力升高时会反复确认关键细节。",
                        "voice_profile": {
                            "style": "克制",
                            "sentence_pattern": "短句",
                            "speech_tempo": "normal",
                            "metaphors": [],
                            "catchphrases": [],
                        },
                        "active_wounds": [
                            {"description": "旧选择留下的压力", "trigger": "类似代价再次出现", "effect": "先控制信息再行动"}
                        ],
                        "relationships": [],
                    },
                    {
                        "name": "关键关系乙",
                        "gender": "未指定",
                        "age": "未指定",
                        "role": "盟友",
                        "description": "提供不同判断标准，与核心人物形成互补或分歧。",
                        "appearance": "",
                        "personality": "先审视风险，再决定是否靠近。",
                        "background": "过去曾因为轻信而遭受损失。",
                        "public_profile": "与核心问题存在明确关联。",
                        "hidden_profile": "",
                        "reveal_chapter": None,
                        "mental_state": "观望",
                        "mental_state_reason": "尚未确认核心人物是否值得合作。",
                        "core_belief": "合作必须建立在可验证事实上。",
                        "moral_taboos": ["不无条件服从", "不隐瞒致命风险"],
                        "core_motivation": "确认局势真相。",
                        "inner_lack": "建立可持续的信任关系。",
                        "ghost": "曾因轻信付出代价。",
                        "want": "确认局势真相。",
                        "need": "建立可持续的信任关系。",
                        "flaw": "过度防御。",
                        "verbal_tic": "",
                        "idle_behavior": "先观察出口和风险点。",
                        "voice_profile": {
                            "style": "谨慎",
                            "sentence_pattern": "反问",
                            "speech_tempo": "normal",
                            "metaphors": [],
                            "catchphrases": [],
                        },
                        "active_wounds": [],
                        "relationships": [
                            {"target": "核心人物甲", "relation": "合作", "description": "信任需要通过行动逐步建立。"}
                        ],
                    },
                    {
                        "name": "阻力人物丙",
                        "gender": "未指定",
                        "age": "未指定",
                        "role": "对立角色",
                        "description": "代表阻止目标达成的现实力量或价值立场。",
                        "appearance": "",
                        "personality": "控制欲强，习惯通过压力掌握节奏。",
                        "background": "长期处于必须维持秩序的位置。",
                        "public_profile": "拥有制造障碍的资源、权限或信息优势。",
                        "hidden_profile": "真实动机需由后续剧情确认。",
                        "reveal_chapter": None,
                        "mental_state": "施压",
                        "mental_state_reason": "核心人物的行动影响其既有利益或秩序。",
                        "core_belief": "秩序比个体选择更重要。",
                        "moral_taboos": ["不公开承认失控", "不轻易交出主动权"],
                        "core_motivation": "维持现有优势。",
                        "inner_lack": "面对变化并重新定义秩序。",
                        "ghost": "失去控制感。",
                        "want": "维持现有优势。",
                        "need": "面对变化并重新定义秩序。",
                        "flaw": "低估个体行动的连锁反应。",
                        "verbal_tic": "",
                        "idle_behavior": "用沉默迫使对方先暴露需求。",
                        "voice_profile": {
                            "style": "压迫",
                            "sentence_pattern": "命令式",
                            "speech_tempo": "slow",
                            "metaphors": [],
                            "catchphrases": [],
                        },
                        "active_wounds": [],
                        "relationships": [
                            {"target": "核心人物甲", "relation": "阻力", "description": "围绕目标、代价和规则解释权形成对抗。"}
                        ],
                    },
                ]
            }
        )

    def _locations(self) -> str:
        return self._json(
            {
                "locations": [
                    {
                        "id": "location_starting_point",
                        "name": "起始地点",
                        "type": "区域",
                        "description": "核心问题第一次显性化的地点，承担开局压力与信息投放功能。",
                        "parent_id": None,
                        "connections": [
                            {"target": "关键转折地点", "relation": "通往", "description": "行动从发现问题转向验证问题。"}
                        ],
                    },
                    {
                        "id": "location_turning_point",
                        "name": "关键转折地点",
                        "type": "场所",
                        "description": "人物必须付出代价或做出选择的地点，推动目标升级。",
                        "parent_id": None,
                        "connections": [
                            {"target": "结果承压地点", "relation": "通往", "description": "选择产生后果并扩散到更大范围。"}
                        ],
                    },
                    {
                        "id": "location_consequence_point",
                        "name": "结果承压地点",
                        "type": "区域",
                        "description": "集中呈现阶段后果、关系变化和下一轮冲突入口。",
                        "parent_id": None,
                        "connections": [],
                    },
                ]
            }
        )

    def _main_plot_options(self) -> str:
        return self._json(
            {
                "plot_options": [
                    {
                        "id": "mock_option_goal_pressure",
                        "type": "目标压力型",
                        "title": "目标被迫提前",
                        "logline": "核心人物为了处理用户设定中的关键问题，必须在准备不足时提前行动。",
                        "core_conflict": "个人目标与外部压力之间的冲突。",
                        "starting_hook": "一个无法延后的后果迫使核心人物立刻做选择。",
                    },
                    {
                        "id": "mock_option_relationship_tension",
                        "type": "关系张力型",
                        "title": "信任需要代价",
                        "logline": "核心人物需要争取关键关系的协助，却必须先证明自己愿意承担代价。",
                        "core_conflict": "合作需求与信任缺口之间的冲突。",
                        "starting_hook": "最需要合作的时刻，对方提出了一个必须当场回应的条件。",
                    },
                    {
                        "id": "mock_option_rule_boundary",
                        "type": "规则边界型",
                        "title": "规则露出裂缝",
                        "logline": "既有规则无法解释新出现的问题，核心人物因此进入更大的冲突结构。",
                        "core_conflict": "旧规则的稳定性与新问题的破坏性之间的冲突。",
                        "starting_hook": "一次按规则执行的行动产生了反常结果。",
                    },
                ]
            }
        )

    def _plot_outline(self) -> str:
        overview = (
            "故事从主角在既有秩序中被迫面对一个无法回避的现实缺口开始："
            "原本可被拖延的问题在一次外部事件后突然前置，主角必须立刻行动。"
            "他试图先用最小代价保住当下的重要关系与资源，却发现真正的冲突并不只是局部困境，"
            "而是世界规则、权力结构与个人选择之间的持续拉扯。随着调查、试探与对抗推进，"
            "主角会一步步意识到自己面对的是一条会不断升级的主线压力链：每做出一次选择，"
            "都要在短期得失、关系信任和更长期的目标之间承担新的代价。故事中段，"
            "关键角色与核心地点会不断把表层问题导向更深层真相，迫使主角从被动应对转为主动突破。"
            "后段则把前文积累的矛盾集中兑现，让主角在最不利条件下完成立场确认、代价支付与最终决断，"
            "并为结局阶段留下清晰的收束方向。"
        )
        return self._json(
            {
                "plot_outline": {
                    "main_story_overview": overview,
                    "stage_plan": [
                        {
                            "phase": "opening",
                            "label": "开篇阶段",
                            "range_percent": "1-15%",
                            "summary": "建立主角的初始处境、核心缺口与第一轮外部压力，让主线问题快速显性化。",
                            "key_goals": ["建立主角目标", "引入核心冲突", "给出第一章钩子"],
                        },
                        {
                            "phase": "development",
                            "label": "发展阶段",
                            "range_percent": "15-40%",
                            "summary": "通过连续受阻与局势扩张，把局部问题推向更大范围的对抗结构。",
                            "key_goals": ["升级外部压力", "拉开关系张力", "明确阶段代价"],
                        },
                        {
                            "phase": "deepening",
                            "label": "深化阶段",
                            "range_percent": "40-70%",
                            "summary": "推进关键真相、人物成长与立场变化，让主线矛盾进入不可回避的深水区。",
                            "key_goals": ["揭示关键真相", "迫使人物转变", "压缩退路"],
                        },
                        {
                            "phase": "climax",
                            "label": "高潮阶段",
                            "range_percent": "70-90%",
                            "summary": "集中兑现前文矛盾与筹码，把主角推入必须决断的最高潮对抗。",
                            "key_goals": ["集中冲突", "支付代价", "完成决断"],
                        },
                        {
                            "phase": "ending",
                            "label": "收尾阶段",
                            "range_percent": "90-100%",
                            "summary": "收束主线后果与人物去向，为故事结局提供明确且连贯的闭环。",
                            "key_goals": ["回收线索", "稳定新秩序", "落地结局"],
                        },
                    ],
                    "expected_ending": "主角在付出明确代价后完成主线目标的一次阶段性兑现，并让世界秩序或人物关系进入新的稳定状态。",
                    "core_conflict": "主角想守住自身目标与重要关系，但外部秩序和更大的结构性压力不断要求他付出超出预期的代价。",
                }
            }
        )

    def _chapter_review(self) -> str:
        return self._json(
            {
                "status": "reviewed",
                "score": 75,
                "summary": "本地模拟审阅只验证结构契约，真实质量判断需要配置 LLM。",
                "issues": [
                    {
                        "severity": "suggestion",
                        "location": "全文",
                        "description": "当前为无密钥环境的结构化模拟结果。",
                        "suggestion": "配置真实模型后重新执行 AI 审阅。",
                    }
                ],
                "suggestions": ["配置真实模型后重新执行 AI 审阅。"],
            }
        )

    def _outline_cohort(self) -> str:
        """Return a valid generic sibling cohort for no-key/local runs."""

        payloads = []
        previous_exit = ""
        for index in range(1, 4):
            exit_state = f"阶段 {index} 的目标已完成，并留下下一阶段的明确入口。"
            payloads.append(
                {
                    "title": f"阶段 {index}",
                    "narrative_text": "围绕当前父级目标推进一轮可验证的压力、选择与代价。",
                    "creative_goal": "完成本阶段的核心推进并明确下一阶段入口。",
                    "entry_state": "本阶段开始时，核心目标尚未完成。"
                    if index == 1
                    else previous_exit,
                    "exit_state": exit_state,
                    "conflicts": ["目标推进与新增代价之间的冲突"],
                    "state_changes": {
                        "story": [{"change": f"阶段 {index} 的目标、阻力与代价发生可见变化。"}]
                    },
                    "handoff_conditions": ["下一阶段必须承接本阶段的结果与新增代价。"],
                    "chapter_start": index,
                    "chapter_end": index,
                }
            )
            previous_exit = exit_state
        return json.dumps(payloads, ensure_ascii=False, separators=(",", ":"))

    def _act_plan(self, prompt: Prompt) -> str:
        match = re.search(r"请为这一幕规划\s*(\d+)\s*个章节", prompt.user)
        chapter_count = max(int(match.group(1)), 1) if match else 1
        chapters = []
        for number in range(1, chapter_count + 1):
            is_first = number == 1
            is_last = number == chapter_count
            chapters.append(
                {
                    "number": number,
                    "title": f"第{number}章：压力推进",
                    "main_event": "围绕当前核心压力做出行动选择，并产生可见后果。",
                    "handoff_from_previous": (
                        "从本幕入口落下当前核心压力。"
                        if is_first
                        else "承接上一章行动留下的后果和未解问题。"
                    ),
                    "handoff_to_next": (
                        "将本幕的阶段性后果交给下一幕。"
                        if is_last
                        else "留下必须由下一章回应的明确压力。"
                    ),
                    "required_threads": ["当前幕核心问题"],
                    "location_hint": "当前冲突地点",
                    "cast_hint": ["核心人物甲"],
                    "thrill_type": "hook" if is_first else "action",
                    "thrill_description": "通过选择和即时后果提供结构化正反馈。",
                    "foreshadow_action": "resolve" if is_last else "plant",
                    "foreshadow_detail": "当前行动留下的线索将在后续章节回应。",
                }
            )
        return self._json({"chapters": chapters})

    def _chapter_preplan(self) -> str:
        return self._json(
            {
                "detail_title": "压力落地与主动选择",
                "key_plot_points": [
                    "当前压力在具体场景中落地。",
                    "核心人物甲确认无法回避的代价。",
                    "关键关系乙提出风险判断。",
                    "核心人物甲做出可验证的主动选择。",
                    "选择产生下一章必须回应的新问题。",
                ],
                "chapter_characters": ["核心人物甲", "关键关系乙"],
                "chapter_plan": {
                    "opening_entry": "核心人物甲在当前冲突地点核对一条会改变处境的信息。",
                    "scene_transitions": [
                        {
                            "scene": "压力落场",
                            "location": "当前冲突地点",
                            "cast": ["核心人物甲"],
                            "purpose": "让当前主事件变成必须回应的即时压力。",
                        },
                        {
                            "scene": "风险对照",
                            "location": "当前冲突地点",
                            "cast": ["核心人物甲", "关键关系乙"],
                            "purpose": "明确选择代价与可用筹码。",
                        },
                    ],
                    "key_dialogues": [
                        {
                            "speaker": "核心人物甲",
                            "line": "先确认这条信息会让谁失去选择。",
                            "reply": "关键关系乙要求先核对风险。",
                            "purpose": "建立行动目标与风险分歧。",
                        },
                        {
                            "speaker": "关键关系乙",
                            "line": "一旦行动，代价不会只落在你身上。",
                            "reply": "核心人物甲要求给出可验证的退路。",
                            "purpose": "压实选择代价。",
                        },
                        {
                            "speaker": "核心人物甲",
                            "line": "我不接受把无关者当作代价。",
                            "reply": "关键关系乙指出时间窗口正在关闭。",
                            "purpose": "落实人物底线和外部压力。",
                        },
                        {
                            "speaker": "核心人物甲",
                            "line": "那就用现有筹码先打开一个缺口。",
                            "reply": "关键关系乙同意协助验证。",
                            "purpose": "把讨论转化为共同动作。",
                        },
                    ],
                    "event_chain": [
                        {"phase": "触发", "content": "核心人物甲发现当前压力已经影响到可争夺资源。"},
                        {"phase": "升级", "content": "关键关系乙补充风险信息，证明拖延会扩大后果。"},
                        {"phase": "升级", "content": "两人比对现有筹码，排除一条会伤及无关者的方案。"},
                        {"phase": "爆发", "content": "核心人物甲选择先验证关键入口，而不是继续等待。"},
                        {"phase": "爆发", "content": "行动获得局部信息回报，同时暴露新的阻力。"},
                        {"phase": "收束", "content": "核心人物甲确认下一章必须回应新阻力并保护当前筹码。"},
                    ],
                    "character_decisions": [
                        {
                            "actor": "核心人物甲",
                            "decision": "在时间窗口关闭前验证关键入口。",
                            "purpose": "用可控风险换取下一步行动所需的信息。",
                        }
                    ],
                    "payoff_reversals": [
                        "预期只能被动承压，反转为核心人物甲主动拿到可验证线索，形成即时正反馈。"
                    ],
                    "protagonist_state_change": {
                        "位置": "当前冲突地点",
                        "实力": "无直接变化，但获得可执行的信息优势。",
                        "新获得": "关键入口的验证线索。",
                        "身体状况": "承压但可行动。",
                        "重大变化": "从等待风险落地转为主动验证下一步。",
                    },
                },
            }
        )

    def _prose(self, prompt: Prompt) -> str:
        match = re.search(r"文章字数\s*[：:]\s*(\d+)", prompt.user)
        target_length = int(match.group(1)) if match else 2000
        target_length = min(max(target_length, 800), 5000)
        paragraphs = [
            "当前冲突地点的光线被临时警报切成几段，核心人物甲没有立刻行动。"
            "他先核对手里的线索，又确认身边的人是否仍有选择，这让眼前的压力不再只是一个抽象的威胁。",
            "关键关系乙指出时间窗口正在收紧，任何看似省事的方案都会把代价转给无关的人。"
            "核心人物甲因此放弃了最快的路径，转而把现有筹码拆成可以逐项验证的步骤。",
            "第一次验证带来了局部回报：关键入口确实存在，但入口另一端也留下了新的阻力。"
            "两人没有把这当成胜利，而是把得到的信息同下一步要承担的风险一并记下。",
            "核心人物甲选择先守住能够改变局面的证据，再用一次明确行动回应压力。"
            "当场的选择没有替他解决全部问题，却让后续行动终于有了可追溯的方向和必须兑现的代价。",
        ]
        blocks: list[str] = []
        while len("\n\n".join(blocks)) < target_length:
            blocks.extend(paragraphs)
        return "\n\n".join(blocks)

    def _narrative_sync(self) -> str:
        return self._json(
            {
                "summary": (
                    "核心人物甲在当前冲突地点核对关键信息，拒绝把无关者当作代价，"
                    "并与关键关系乙共同验证了一个可用入口。行动带来局部线索，也暴露出"
                    "必须在下一章回应的新阻力。"
                ),
                "key_events": "核对压力来源；确认风险；验证关键入口；获得局部线索；暴露新阻力。",
                "open_threads": "关键入口另一端的阻力来源尚未确认，现有筹码需要在下一章得到保护。",
                "relation_triples": [
                    {
                        "subject": "核心人物甲",
                        "predicate": "协作",
                        "object": "关键关系乙",
                    }
                ],
                "foreshadow_hints": [
                    {
                        "description": "关键入口另一端的阻力来源",
                        "suggested_resolve_offset": 3,
                        "importance": "medium",
                        "resolve_hint": "下一幕前确认阻力的具体目的",
                    }
                ],
                "consumed_foreshadows": [],
                "storyline_progress": [
                    {
                        "type": "主线",
                        "arc_label": "入口阻力",
                        "description": "主角从被动承压转为主动验证，并获得下一步线索。",
                    }
                ],
                "dialogues": [
                    {
                        "speaker": "核心人物甲",
                        "content": "先确认这条信息会让谁失去选择。",
                        "context": "风险对照",
                    }
                ],
                "timeline_events": [
                    {
                        "time_point": "本章",
                        "event": "验证关键入口",
                        "description": "核心人物甲与关键关系乙完成首次可控验证。",
                    }
                ],
                "causal_edges": [
                    {
                        "source_event": "关键入口被验证",
                        "causal_type": "triggers",
                        "target_event": "新阻力需要被回应",
                        "state_change": "核心人物甲从等待风险转为主动行动。",
                        "involved_characters": ["核心人物甲", "关键关系乙"],
                        "strength": 0.8,
                    }
                ],
                "character_mutations": [
                    {
                        "character_name": "核心人物甲",
                        "mutation_type": "motivation",
                        "source_event": "关键入口验证后暴露新阻力",
                        "impact_or_description": "决定保护筹码并查明阻力来源。",
                        "sensitivity_tags_or_priority": 7,
                        "intensity": 7,
                    }
                ],
                "character_states": [
                    {
                        "character_name": "核心人物甲",
                        "mental_state": "确认风险后保持克制，决心主动验证下一步。",
                    },
                    {
                        "character_name": "关键关系乙",
                        "mental_state": "认可合作的必要性，同时持续警惕代价扩散。",
                    },
                ],
            }
        )

    def _memory_extraction(self, prompt: Prompt) -> str:
        match = re.search(r"第\s*(\d+)\s*章", prompt.user)
        chapter_number = int(match.group(1)) if match else 1
        return self._json(
            {
                "completed_beats": [
                    {
                        "beat_id": f"mock-ch{chapter_number}-choice",
                        "summary": "核心人物甲在压力下做出可追溯的选择，并留下下一步需要回应的阻力。",
                        "chapter": chapter_number,
                        "characters_involved": ["核心人物甲", "关键关系乙"],
                    }
                ],
                "revealed_clues": [
                    {
                        "clue_id": f"mock-clue-ch{chapter_number}-entry",
                        "content": "关键入口存在，但其另一端的阻力仍需查明。",
                        "revealed_at_chapter": chapter_number,
                        "category": "truth",
                        "is_still_valid": True,
                    }
                ],
                "fact_violations": [],
            }
        )

    def _macro_refactor(self) -> str:
        return self._json(
            {
                "natural_language_suggestion": (
                    "本地模拟仅确认提案契约；请在配置真实模型后生成针对当前事件的具体改写建议。"
                ),
                "suggested_mutations": [],
                "suggested_tags": [],
                "reasoning": (
                    "无密钥模式不虚构人物、动机或标签变更，以免模拟结果被误用为真实叙事修改。"
                ),
            }
        )

    def _style(self) -> str:
        return self._style_text()

    def _style_text(self) -> str:
        return "第三人称有限视角，叙事聚焦人物选择、信息差和代价反馈；节奏清晰，避免无效铺陈。"

    def _default(self) -> str:
        return self._json(
            {
                "characters": [],
                "locations": [],
                "style": self._style_text(),
                "worldbuilding": {},
                "parts": [],
                "plot_options": [],
                "plot_outline": {},
            }
        )


class MockProvider(LLMService):
    """Mock LLM provider for tests and local no-key runs."""

    def __init__(self, response_factory: MockResponseFactory | None = None):
        self._response_factory = response_factory or MockResponseFactory()

    async def generate(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        content = self._response_factory.build(prompt)
        token_usage = TokenUsage(
            input_tokens=len(prompt.system) + len(prompt.user),
            output_tokens=len(content),
        )
        return GenerationResult(content=content, token_usage=token_usage)

    async def stream_generate(self, prompt: Prompt, config: GenerationConfig) -> AsyncIterator[str]:
        result = await self.generate(prompt, config)
        chunk_size = 50
        for index in range(0, len(result.content), chunk_size):
            yield result.content[index : index + chunk_size]
