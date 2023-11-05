from typing import List, Tuple

from langchain.agents.format_scratchpad import format_xml
from langchain.schema import AIMessage, HumanMessage
from langchain.tools.render import render_text_description

from .prompts import conversational_prompt, parse_output


def _format_chat_history(chat_history: List[Tuple[str, str]]):
    buffer = []
    for human, ai in chat_history:
        buffer.append(HumanMessage(content=human))
        buffer.append(AIMessage(content=ai))
    return buffer


def get_xml_agent(model, tools, system_message):
    prompt = conversational_prompt.partial(
        tools=render_text_description(tools),
        tool_names=", ".join([t.name for t in tools]),
        system_message=system_message,
    )
    llm_with_stop = model.bind(stop=["</tool_input>"])

    agent = (
        {
            "question": lambda x: x["question"],
            "agent_scratchpad": lambda x: format_xml(x["intermediate_steps"]),
            "chat_history": lambda x: _format_chat_history(x["chat_history"]),
        }
        | prompt
        | llm_with_stop
        | parse_output
    )
    return agent
