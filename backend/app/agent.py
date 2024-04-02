import random
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Union

from langchain_core.messages import AnyMessage
from langchain_core.runnables import (
    ConfigurableField,
    RunnableBinding,
)
from langgraph.checkpoint import CheckpointAt
from langsmith import Client as LangSmithClient
from langsmith.schemas import Example

from app.agent_types.google_agent import get_google_agent_executor
from app.agent_types.openai_agent import get_openai_agent_executor
from app.agent_types.xml_agent import get_xml_agent_executor
from app.chatbot import get_chatbot_executor
from app.checkpoint import PostgresCheckpoint
from app.llms import (
    get_anthropic_llm,
    get_google_llm,
    get_mixtral_fireworks,
    get_openai_llm,
)
from app.retrieval import get_retrieval_executor
from app.tools import (
    RETRIEVAL_DESCRIPTION,
    TOOLS,
    ActionServer,
    Arxiv,
    AvailableTools,
    Connery,
    DDGSearch,
    PressReleases,
    PubMed,
    Retrieval,
    SecFilings,
    Tavily,
    TavilyAnswer,
    Wikipedia,
    YouSearch,
    get_retrieval_tool,
    get_retriever,
)

Tool = Union[
    ActionServer,
    Connery,
    DDGSearch,
    Arxiv,
    YouSearch,
    SecFilings,
    PressReleases,
    PubMed,
    Wikipedia,
    Tavily,
    TavilyAnswer,
    Retrieval,
]


class AgentType(str, Enum):
    GPT_35_TURBO = "GPT 3.5 Turbo"
    GPT_4 = "GPT 4 Turbo"
    AZURE_OPENAI = "GPT 4 (Azure OpenAI)"
    CLAUDE2 = "Claude 2"
    BEDROCK_CLAUDE2 = "Claude 2 (Amazon Bedrock)"
    GEMINI = "GEMINI"


DEFAULT_SYSTEM_MESSAGE = "You are a helpful assistant."

CHECKPOINTER = PostgresCheckpoint(at=CheckpointAt.END_OF_STEP)


def _format_chat_example(example: Example) -> str:
    feedback = ""
    for i in example.inputs["input"][1:]:
        if i["type"] == "human":
            feedback += "<human_feedback>\n" + i["content"] + "\n</human_feedback>\n"
    return f"""<original_input>
{example.inputs['input'][0]['content']}
</original_input>
{feedback}<output>
{example.outputs['output']['content']}
</output>"""


def _format_agent_example(example: Example) -> str:
    new_messages = []
    for o in example.outputs["output"][1:][::-1]:
        if o["type"] == "human":
            break
        new_messages.append(o)
    return f"""<trajectory>
{[example.outputs['output'][0]] + new_messages[::-1]}
</trajectory>"""


def get_few_shot_str(assistant_id: str, *, agent: bool = False) -> str:
    client = LangSmithClient()
    if client.has_dataset(dataset_name=assistant_id):
        # TODO: Update to randomize
        examples = list(client.list_examples(dataset_name=assistant_id))
        if not examples:
            return ""
        if agent:
            examples = random.sample(examples, min(len(examples), 10))
            example_str = "\n".join([_format_agent_example(e) for e in examples])
        else:
            examples = random.sample(examples, min(len(examples), 10))
            example_str = "\n".join([_format_chat_example(e) for e in examples])
        return f"""

Here are some previous interactions with a user trying to accomplish a similar task. \
You should assume that the final output is the desired one, and any \
intermediate steps were wrong in some way, and the human then tried to improve upon \
them in specific ways. Learn from these previous interactions and do not repeat past \
mistakes!

{example_str}
"""


def get_agent_executor(
    tools: list,
    agent: AgentType,
    system_message: str,
    interrupt_before_action: bool,
    *,
    assistant_id: Optional[str] = None,
):
    if assistant_id is not None:
        system_message += get_few_shot_str(assistant_id, agent=True)

    if agent == AgentType.GPT_35_TURBO:
        llm = get_openai_llm()
        return get_openai_agent_executor(
            tools, llm, system_message, interrupt_before_action, CHECKPOINTER
        )
    elif agent == AgentType.GPT_4:
        llm = get_openai_llm(gpt_4=True)
        return get_openai_agent_executor(
            tools, llm, system_message, interrupt_before_action, CHECKPOINTER
        )
    elif agent == AgentType.AZURE_OPENAI:
        llm = get_openai_llm(azure=True)
        return get_openai_agent_executor(
            tools, llm, system_message, interrupt_before_action, CHECKPOINTER
        )
    elif agent == AgentType.CLAUDE2:
        llm = get_anthropic_llm()
        return get_xml_agent_executor(
            tools, llm, system_message, interrupt_before_action, CHECKPOINTER
        )
    elif agent == AgentType.BEDROCK_CLAUDE2:
        llm = get_anthropic_llm(bedrock=True)
        return get_xml_agent_executor(
            tools, llm, system_message, interrupt_before_action, CHECKPOINTER
        )
    elif agent == AgentType.GEMINI:
        llm = get_google_llm()
        return get_google_agent_executor(
            tools, llm, system_message, interrupt_before_action, CHECKPOINTER
        )
    else:
        raise ValueError("Unexpected agent type")


class ConfigurableAgent(RunnableBinding):
    tools: Sequence[Tool]
    agent: AgentType
    system_message: str = DEFAULT_SYSTEM_MESSAGE
    retrieval_description: str = RETRIEVAL_DESCRIPTION
    interrupt_before_action: bool = False
    assistant_id: Optional[str] = None
    thread_id: Optional[str] = None
    user_id: Optional[str] = None

    def __init__(
        self,
        *,
        tools: Sequence[Tool],
        agent: AgentType = AgentType.GPT_35_TURBO,
        system_message: str = DEFAULT_SYSTEM_MESSAGE,
        assistant_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        retrieval_description: str = RETRIEVAL_DESCRIPTION,
        interrupt_before_action: bool = False,
        kwargs: Optional[Mapping[str, Any]] = None,
        config: Optional[Mapping[str, Any]] = None,
        **others: Any,
    ) -> None:
        others.pop("bound", None)
        _tools = []
        for _tool in tools:
            if _tool["type"] == AvailableTools.RETRIEVAL:
                if assistant_id is None or thread_id is None:
                    raise ValueError(
                        "Both assistant_id and thread_id must be provided if Retrieval tool is used"
                    )
                _tools.append(
                    get_retrieval_tool(assistant_id, thread_id, retrieval_description)
                )
            else:
                tool_config = _tool.get("config", {})
                _returned_tools = TOOLS[_tool["type"]](**tool_config)
                if isinstance(_returned_tools, list):
                    _tools.extend(_returned_tools)
                else:
                    _tools.append(_returned_tools)
        _agent = get_agent_executor(
            _tools,
            agent,
            system_message,
            interrupt_before_action,
            assistant_id=assistant_id,
        )
        agent_executor = _agent.with_config({"recursion_limit": 50})
        super().__init__(
            tools=tools,
            agent=agent,
            system_message=system_message,
            retrieval_description=retrieval_description,
            bound=agent_executor,
            kwargs=kwargs or {},
            config=config or {},
        )


class LLMType(str, Enum):
    GPT_35_TURBO = "GPT 3.5 Turbo"
    GPT_4 = "GPT 4 Turbo"
    AZURE_OPENAI = "GPT 4 (Azure OpenAI)"
    CLAUDE2 = "Claude 2"
    BEDROCK_CLAUDE2 = "Claude 2 (Amazon Bedrock)"
    GEMINI = "GEMINI"
    MIXTRAL = "Mixtral"


def get_chatbot(
    llm_type: LLMType,
    system_message: str,
    *,
    assistant_id: Optional[str] = None,
):
    if llm_type == LLMType.GPT_35_TURBO:
        llm = get_openai_llm()
    elif llm_type == LLMType.GPT_4:
        llm = get_openai_llm(gpt_4=True)
    elif llm_type == LLMType.AZURE_OPENAI:
        llm = get_openai_llm(azure=True)
    elif llm_type == LLMType.CLAUDE2:
        llm = get_anthropic_llm()
    elif llm_type == LLMType.BEDROCK_CLAUDE2:
        llm = get_anthropic_llm(bedrock=True)
    elif llm_type == LLMType.GEMINI:
        llm = get_google_llm()
    elif llm_type == LLMType.MIXTRAL:
        llm = get_mixtral_fireworks()
    else:
        raise ValueError("Unexpected llm type")

    if assistant_id:
        system_message += get_few_shot_str(assistant_id)

    return get_chatbot_executor(llm, system_message, CHECKPOINTER)


class ConfigurableChatBot(RunnableBinding):
    llm: LLMType
    system_message: str = DEFAULT_SYSTEM_MESSAGE
    user_id: Optional[str] = None
    assistant_id: Optional[str] = None

    def __init__(
        self,
        *,
        llm: LLMType = LLMType.GPT_35_TURBO,
        system_message: str = DEFAULT_SYSTEM_MESSAGE,
        assistant_id: Optional[str] = None,
        kwargs: Optional[Mapping[str, Any]] = None,
        config: Optional[Mapping[str, Any]] = None,
        **others: Any,
    ) -> None:
        others.pop("bound", None)

        chatbot = get_chatbot(llm, system_message, assistant_id=assistant_id)
        super().__init__(
            llm=llm,
            system_message=system_message,
            bound=chatbot,
            kwargs=kwargs or {},
            config=config or {},
        )


chatbot = (
    ConfigurableChatBot(llm=LLMType.GPT_35_TURBO, checkpoint=CHECKPOINTER)
    .configurable_fields(
        llm=ConfigurableField(id="llm_type", name="LLM Type"),
        system_message=ConfigurableField(id="system_message", name="Instructions"),
        assistant_id=ConfigurableField(
            id="assistant_id", name="Assistant ID", is_shared=True
        ),
    )
    .with_types(input_type=Sequence[AnyMessage], output_type=Sequence[AnyMessage])
)


class ConfigurableRetrieval(RunnableBinding):
    llm_type: LLMType
    system_message: str = DEFAULT_SYSTEM_MESSAGE
    assistant_id: Optional[str] = None
    thread_id: Optional[str] = None
    user_id: Optional[str] = None

    def __init__(
        self,
        *,
        llm_type: LLMType = LLMType.GPT_35_TURBO,
        system_message: str = DEFAULT_SYSTEM_MESSAGE,
        assistant_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        kwargs: Optional[Mapping[str, Any]] = None,
        config: Optional[Mapping[str, Any]] = None,
        **others: Any,
    ) -> None:
        others.pop("bound", None)
        retriever = get_retriever(assistant_id, thread_id)
        if llm_type == LLMType.GPT_35_TURBO:
            llm = get_openai_llm()
        elif llm_type == LLMType.GPT_4:
            llm = get_openai_llm(gpt_4=True)
        elif llm_type == LLMType.AZURE_OPENAI:
            llm = get_openai_llm(azure=True)
        elif llm_type == LLMType.CLAUDE2:
            llm = get_anthropic_llm()
        elif llm_type == LLMType.BEDROCK_CLAUDE2:
            llm = get_anthropic_llm(bedrock=True)
        elif llm_type == LLMType.GEMINI:
            llm = get_google_llm()
        elif llm_type == LLMType.MIXTRAL:
            llm = get_mixtral_fireworks()
        else:
            raise ValueError("Unexpected llm type")
        chatbot = get_retrieval_executor(llm, retriever, system_message, CHECKPOINTER)
        super().__init__(
            llm_type=llm_type,
            system_message=system_message,
            bound=chatbot,
            kwargs=kwargs or {},
            config=config or {},
        )


chat_retrieval = (
    ConfigurableRetrieval(llm_type=LLMType.GPT_35_TURBO, checkpoint=CHECKPOINTER)
    .configurable_fields(
        llm_type=ConfigurableField(id="llm_type", name="LLM Type"),
        system_message=ConfigurableField(id="system_message", name="Instructions"),
        assistant_id=ConfigurableField(
            id="assistant_id", name="Assistant ID", is_shared=True
        ),
        thread_id=ConfigurableField(id="thread_id", name="Thread ID", is_shared=True),
    )
    .with_types(input_type=Sequence[AnyMessage], output_type=Sequence[AnyMessage])
)


agent_w_tools = (
    ConfigurableAgent(
        agent=AgentType.GPT_35_TURBO,
        tools=[],
        system_message=DEFAULT_SYSTEM_MESSAGE,
        retrieval_description=RETRIEVAL_DESCRIPTION,
        assistant_id=None,
        thread_id=None,
    )
    .configurable_fields(
        agent=ConfigurableField(id="agent_type", name="Agent Type"),
        system_message=ConfigurableField(id="system_message", name="Instructions"),
        interrupt_before_action=ConfigurableField(
            id="interrupt_before_action",
            name="Tool Confirmation",
            description="If Yes, you'll be prompted to continue before each tool is executed.\nIf No, tools will be executed automatically by the agent.",
        ),
        assistant_id=ConfigurableField(
            id="assistant_id", name="Assistant ID", is_shared=True
        ),
        thread_id=ConfigurableField(id="thread_id", name="Thread ID", is_shared=True),
        tools=ConfigurableField(id="tools", name="Tools"),
        retrieval_description=ConfigurableField(
            id="retrieval_description", name="Retrieval Description"
        ),
    )
    .with_types(input_type=Sequence[AnyMessage], output_type=Sequence[AnyMessage])
)


agent = agent_w_tools.configurable_alternatives(
    ConfigurableField(id="type", name="Bot Type"),
    default_key="agent",
    prefix_keys=True,
    chatbot=chatbot,
    chat_retrieval=chat_retrieval,
).with_types(input_type=Sequence[AnyMessage], output_type=Sequence[AnyMessage])

if __name__ == "__main__":
    import asyncio

    from langchain.schema.messages import HumanMessage

    async def run():
        async for m in agent.astream_events(
            HumanMessage(content="whats your name"),
            config={"configurable": {"user_id": "2", "thread_id": "test1"}},
            version="v1",
        ):
            print(m)

    asyncio.run(run())
