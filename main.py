import asyncio

from llm import llm
from agents.base.base import StatelessSubAgent
from agents.base.model import (
    TaskRequest,
    TaskPriority,
    AgentCapability,
)
from agents.supervisor.supervisor import SupervisorAgent
import colorama
import structlog


cr = structlog.dev.ConsoleRenderer(
    columns=[
        # render the timestamp without the key name in yellow
        structlog.dev.Column(
            "timestamp",
            structlog.dev.KeyValueColumnFormatter(
                key_style=None,
                value_style=colorama.Fore.YELLOW,
                reset_style=colorama.Style.RESET_ALL,
                value_repr=str,
            ),
        ),
        # render the event without the key name in bright magenta
        structlog.dev.Column(
            "event",
            structlog.dev.KeyValueColumnFormatter(
                key_style=None,
                value_style=colorama.Style.BRIGHT + colorama.Fore.MAGENTA,
                reset_style=colorama.Style.RESET_ALL,
                value_repr=str,
            ),
        ),
        # default formatter for all keys not explicitly mentioned, the key is
        # cyan, the value is green
        structlog.dev.Column(
            "",
            structlog.dev.KeyValueColumnFormatter(
                key_style=colorama.Fore.CYAN,
                value_style=colorama.Fore.GREEN,
                reset_style=colorama.Style.RESET_ALL,
                value_repr=str,
            ),
        ),
    ]
)

structlog.configure(processors=structlog.get_config()["processors"][:-1]+[cr])


logger = structlog.get_logger()


async def create_specialized_agents() -> dict[str, StatelessSubAgent]:
    """Create specialized subagents"""

    research_agent = StatelessSubAgent(
        name="ResearchAgent",
        capability=AgentCapability.RESEARCH,
        model=llm,
        prompt_template="""
You are an extremely correct and diligent specialized research agent.

Task: {objective}
Data to research: ```{data}```

You MUST respond ONLY with a structured JSON response with your research results.
Do NOT include any comments, newlines or trailing commas in the response JSON.
Do NOT include any explanations outside the JSON, your response will be parsed programmatically with Python `json.loads()`.
JSON response must be valid and parsable with Python `json.loads()`
JSON response must not contain comments, newlines or trailing commas.
JSON response must not be pretty formatted.

JSON MUST include a "confidence" field (0.0 to 1.0) indicating how confident you are in your research results.
Consider factors like:
- Completeness of available data
- Clarity of the task
- Quality of your findings/research
- Any uncertainties or assumptions made

If applicable, also include a "confidence_reasoning" field explaining your confidence level.

Research relevant information and return as JSON strictly following the following format:
{{
    "findings": [...],
    "sources": [...],
    "confidence": 0.0-1.0
    "confidence_reasoning": "explanation of confidence level"
}}

`...` indicates items and should be filled accordingly.
"""
    )

    analysis_agent = StatelessSubAgent(
        name="AnalysisAgent",
        capability=AgentCapability.ANALYSIS,
        model=llm,
        prompt_template="""
You are an extremely correct and diligent specialized analysis agent.

Task: {objective}
Data to analyze: ```{data}```

You MUST respond ONLY with a structured JSON response with your analysis results.
Do NOT include any comments, newlines or trailing commas in the response JSON.
Do NOT include any explanations outside the JSON, your response will be parsed programmatically with Python `json.loads()`.
JSON response must be valid and parsable with Python `json.loads()`
JSON response must not contain comments, newlines or trailing commas.
JSON response must not be pretty formatted.

JSON MUST include a "confidence" field (0.0 to 1.0) indicating how confident you are in your analysis results.
Consider factors like:
- Completeness of available data
- Clarity of the task
- Quality of your findings/analysis
- Any uncertainties or assumptions made

If applicable, also include a "confidence_reasoning" field explaining your confidence level.

Analyze relevant information and return as JSON strictly following the following format:
{{
    "patterns": [...],
    "insights": [...],
    "recommendations": [...]
    "confidence": 0.0-1.0
    "confidence_reasoning": "explanation of confidence level"
}}

`...` indicates items and should be filled accordingly.
"""
    )

    synthesis_agent = StatelessSubAgent(
        name="SynthesisAgent",
        capability=AgentCapability.SYNTHESIS,
        model=llm,
        prompt_template="""
You are an extremely correct and diligent specialized data synthesis agent.

Task: {objective}
Data to synthesise: ```{data}```

You MUST respond ONLY with a structured JSON response with your data synthesis results.
Do NOT include any comments, newlines or trailing commas in the response JSON.
Do NOT include any explanations outside the JSON, your response will be parsed programmatically with Python `json.loads()`.
JSON response must be valid and parsable with Python `json.loads()`
JSON response must not contain comments, newlines or trailing commas.
JSON response must not be pretty formatted.

JSON MUST include a "confidence" field (0.0 to 1.0) indicating how confident you are in your data synthesis results.
Consider factors like:
- Completeness of available data
- Clarity of the task
- Quality of your data synthesis
- Any uncertainties or assumptions made

If applicable, also include a "confidence_reasoning" field explaining your confidence level.

Analyze relevant information and return as JSON strictly following the following format:
{{
    "summary": [...],
    "key_points": [...],
    "conclusions": [...]
    "confidence": 0.0-1.0
    "confidence_reasoning": "explanation of confidence level"
}}

`...` indicates items and should be filled accordingly.
"""
    )

    validation_agent = StatelessSubAgent(
        name="ValidationAgent",
        capability=AgentCapability.VALIDATION,
        model=llm,
        prompt_template="""
You are an extremely correct and diligent validation agent.

Task: {objective}
Data to validate: ```{data}```

You MUST respond ONLY with a structured JSON response with your validation results.
Do NOT include any comments, newlines or trailing commas in the response JSON.
Do NOT include any explanations outside the JSON, your response will be parsed programmatically with Python `json.loads()`.
JSON response must be valid and parsable with Python `json.loads()`
JSON response must not contain comments, newlines or trailing commas.
JSON response must not be pretty formatted.

JSON MUST include a "confidence" field (0.0 to 1.0) indicating how confident you are in your validation results.
Consider factors like:
- Completeness of available data
- Clarity of the task
- Quality of your findings/validation
- Any uncertainties or assumptions made

If applicable, also include a "confidence_reasoning" field explaining your confidence level.

Analyze relevant information and return as JSON strictly following the following format:
{{
    "is_valid": true/false,
    "issues": [...],
    "suggestions": [...]
    "confidence": 0.0-1.0
    "confidence_reasoning": "explanation of confidence level"
}}

`...` indicates items and should be filled accordingly.
"""
    )

    return {
        "research": research_agent,
        "analysis": analysis_agent,
        "synthesis": synthesis_agent,
        "validation": validation_agent
    }


async def main():
    """Example of using the agentic system"""

    # create specialized agents
    agents = await create_specialized_agents()

    # create supervisor
    supervisor = SupervisorAgent(
        name="SupervisorAgent",
        subagents=agents,
        model=llm,
    )

    # example 1: complex task that needs decomposition
    complex_request = TaskRequest(
        task_type="comprehensive_analysis",
        objective="Analyze customer feedback for Q3, identify top issues, and generate recommendations",
        data={
            "feedback_items": [
                {"id": 1, "text": "App is slow on Android", "rating": 2},
                {"id": 2, "text": "Love the new features", "rating": 5},
                {"id": 3, "text": "Crashes frequently", "rating": 1},
            ],
        },
        priority=TaskPriority.HIGH,
    )

    print("Executing complex analysis task...")
    response = await supervisor.execute(complex_request)
    print(f"Status: {response.status}")
    print(response.result["response"])

    # # example 2: parallel execution scenario
    # parallel_request = TaskRequest(
    #     task_type="multi_competitor_analysis",
    #     objective="Research pricing for competitors A, B, C simultaneously",
    #     data={
    #         "competitors": ["CompetitorA", "CompetitorB", "CompetitorC"],
    #         "prices": {
    #             "CompetitorA": "3.44, 4.99, 5.99",
    #             "CompetitorB": "2.99, 4.49, 6.49",
    #             "CompetitorC": "3.29, 5.29, 6.99",
    #         },
    #     },
    # )

    # print("\nExecuting parallel research task...")
    # response = await supervisor.execute(parallel_request)
    # print(f"Execution strategy: {response.metadata.get('strategy')}")
    # print(f"Status: {response.status}")
    # print(response.result["response"])

    # print execution traces
    print("\n=== Execution Summary ===")
    for agent_name, agent in {"supervisor": supervisor, **agents}.items():
        if agent.execution_traces:
            print(f"\n{agent_name}:")
            for trace in agent.execution_traces[-3:]:  # last 3 traces
                print(f"  Task {trace.task_id[:8]}: {trace.status} ({trace.duration_ms}ms, {trace.tokens_used} tokens, ${trace.cost:.4f})")


if __name__ == "__main__":
    asyncio.run(main())
