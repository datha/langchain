import sys
from operator import itemgetter
from typing import Any, Dict, List, Optional, Sequence, Union, cast
from uuid import UUID

import pytest
from freezegun import freeze_time
from pytest_mock import MockerFixture
from syrupy import SnapshotAssertion

from langchain.callbacks.manager import Callbacks, collect_runs
from langchain.callbacks.tracers.base import BaseTracer
from langchain.callbacks.tracers.log_stream import RunLog, RunLogPatch
from langchain.callbacks.tracers.schemas import Run
from langchain.callbacks.tracers.stdout import ConsoleCallbackHandler
from langchain.chains.question_answering import load_qa_chain
from langchain.chains.summarize import load_summarize_chain
from langchain.chat_models.fake import FakeListChatModel
from langchain.llms.fake import FakeListLLM, FakeStreamingListLLM
from langchain.load.dump import dumpd, dumps
from langchain.output_parsers.list import CommaSeparatedListOutputParser
from langchain.prompts import PromptTemplate
from langchain.prompts.chat import (
    ChatPromptTemplate,
    ChatPromptValue,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate,
)
from langchain.schema.document import Document
from langchain.schema.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
)
from langchain.schema.output_parser import BaseOutputParser, StrOutputParser
from langchain.schema.retriever import BaseRetriever
from langchain.schema.runnable import (
    RouterRunnable,
    Runnable,
    RunnableBranch,
    RunnableConfig,
    RunnableLambda,
    RunnableMap,
    RunnablePassthrough,
    RunnableSequence,
    RunnableWithFallbacks,
)
from langchain.tools.json.tool import JsonListKeysTool, JsonSpec


class FakeTracer(BaseTracer):
    """Fake tracer that records LangChain execution.
    It replaces run ids with deterministic UUIDs for snapshotting."""

    def __init__(self) -> None:
        """Initialize the tracer."""
        super().__init__()
        self.runs: List[Run] = []
        self.uuids_map: Dict[UUID, UUID] = {}
        self.uuids_generator = (
            UUID(f"00000000-0000-4000-8000-{i:012}", version=4) for i in range(10000)
        )

    def _replace_uuid(self, uuid: UUID) -> UUID:
        if uuid not in self.uuids_map:
            self.uuids_map[uuid] = next(self.uuids_generator)
        return self.uuids_map[uuid]

    def _copy_run(self, run: Run) -> Run:
        return run.copy(
            update={
                "id": self._replace_uuid(run.id),
                "parent_run_id": self.uuids_map[run.parent_run_id]
                if run.parent_run_id
                else None,
                "child_runs": [self._copy_run(child) for child in run.child_runs],
                "execution_order": None,
                "child_execution_order": None,
            }
        )

    def _persist_run(self, run: Run) -> None:
        """Persist a run."""

        self.runs.append(self._copy_run(run))


class FakeRunnable(Runnable[str, int]):
    def invoke(
        self,
        input: str,
        config: Optional[RunnableConfig] = None,
    ) -> int:
        return len(input)


class FakeRetriever(BaseRetriever):
    def _get_relevant_documents(
        self,
        query: str,
        *,
        callbacks: Callbacks = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        return [Document(page_content="foo"), Document(page_content="bar")]

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        callbacks: Callbacks = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> List[Document]:
        return [Document(page_content="foo"), Document(page_content="bar")]


def test_schemas(snapshot: SnapshotAssertion) -> None:
    fake = FakeRunnable()  # str -> int

    assert fake.input_schema.schema() == {
        "title": "FakeRunnableInput",
        "type": "string",
    }
    assert fake.output_schema.schema() == {
        "title": "FakeRunnableOutput",
        "type": "integer",
    }

    fake_bound = FakeRunnable().bind(a="b")  # str -> int

    assert fake_bound.input_schema.schema() == {
        "title": "FakeRunnableInput",
        "type": "string",
    }
    assert fake_bound.output_schema.schema() == {
        "title": "FakeRunnableOutput",
        "type": "integer",
    }

    fake_w_fallbacks = FakeRunnable().with_fallbacks((fake,))  # str -> int

    assert fake_w_fallbacks.input_schema.schema() == {
        "title": "FakeRunnableInput",
        "type": "string",
    }
    assert fake_w_fallbacks.output_schema.schema() == {
        "title": "FakeRunnableOutput",
        "type": "integer",
    }

    def typed_lambda_impl(x: str) -> int:
        return len(x)

    typed_lambda = RunnableLambda(typed_lambda_impl)  # str -> int

    assert typed_lambda.input_schema.schema() == {
        "title": "RunnableLambdaInput",
        "type": "string",
    }
    assert typed_lambda.output_schema.schema() == {
        "title": "RunnableLambdaOutput",
        "type": "integer",
    }

    async def typed_async_lambda_impl(x: str) -> int:
        return len(x)

    typed_async_lambda: Runnable = RunnableLambda(typed_async_lambda_impl)  # str -> int

    assert typed_async_lambda.input_schema.schema() == {
        "title": "RunnableLambdaInput",
        "type": "string",
    }
    assert typed_async_lambda.output_schema.schema() == {
        "title": "RunnableLambdaOutput",
        "type": "integer",
    }

    fake_ret = FakeRetriever()  # str -> List[Document]

    assert fake_ret.input_schema.schema() == {
        "title": "FakeRetrieverInput",
        "type": "string",
    }
    assert fake_ret.output_schema.schema() == {
        "title": "FakeRetrieverOutput",
        "type": "array",
        "items": {"$ref": "#/definitions/Document"},
        "definitions": {
            "Document": {
                "title": "Document",
                "description": "Class for storing a piece of text and associated metadata.",  # noqa: E501
                "type": "object",
                "properties": {
                    "page_content": {"title": "Page Content", "type": "string"},
                    "metadata": {"title": "Metadata", "type": "object"},
                },
                "required": ["page_content"],
            }
        },
    }

    fake_llm = FakeListLLM(responses=["a"])  # str -> List[List[str]]

    assert fake_llm.input_schema.schema() == snapshot
    assert fake_llm.output_schema.schema() == {
        "title": "FakeListLLMOutput",
        "type": "string",
    }

    fake_chat = FakeListChatModel(responses=["a"])  # str -> List[List[str]]

    assert fake_chat.input_schema.schema() == snapshot
    assert fake_chat.output_schema.schema() == snapshot

    prompt = PromptTemplate.from_template("Hello, {name}!")

    assert prompt.input_schema.schema() == {
        "title": "PromptInput",
        "type": "object",
        "properties": {"name": {"title": "Name"}},
    }
    assert prompt.output_schema.schema() == snapshot

    prompt_mapper = PromptTemplate.from_template("Hello, {name}!").map()

    assert prompt_mapper.input_schema.schema() == {
        "definitions": {
            "PromptInput": {
                "properties": {"name": {"title": "Name"}},
                "title": "PromptInput",
                "type": "object",
            }
        },
        "items": {"$ref": "#/definitions/PromptInput"},
        "type": "array",
        "title": "RunnableEachInput",
    }
    assert prompt_mapper.output_schema.schema() == snapshot

    list_parser = CommaSeparatedListOutputParser()

    assert list_parser.input_schema.schema() == snapshot
    assert list_parser.output_schema.schema() == {
        "title": "CommaSeparatedListOutputParserOutput",
        "type": "array",
        "items": {"type": "string"},
    }

    seq = prompt | fake_llm | list_parser

    assert seq.input_schema.schema() == {
        "title": "PromptInput",
        "type": "object",
        "properties": {"name": {"title": "Name"}},
    }
    assert seq.output_schema.schema() == {
        "type": "array",
        "items": {"type": "string"},
        "title": "CommaSeparatedListOutputParserOutput",
    }

    router: Runnable = RouterRunnable({})

    assert router.input_schema.schema() == {
        "title": "RouterRunnableInput",
        "$ref": "#/definitions/RouterInput",
        "definitions": {
            "RouterInput": {
                "title": "RouterInput",
                "type": "object",
                "properties": {
                    "key": {"title": "Key", "type": "string"},
                    "input": {"title": "Input"},
                },
                "required": ["key", "input"],
            }
        },
    }
    assert router.output_schema.schema() == {"title": "RouterRunnableOutput"}

    seq_w_map: Runnable = (
        prompt
        | fake_llm
        | {
            "original": RunnablePassthrough(input_type=str),
            "as_list": list_parser,
            "length": typed_lambda_impl,
        }
    )

    assert seq_w_map.input_schema.schema() == {
        "title": "PromptInput",
        "type": "object",
        "properties": {"name": {"title": "Name"}},
    }
    assert seq_w_map.output_schema.schema() == {
        "title": "RunnableMapOutput",
        "type": "object",
        "properties": {
            "original": {"title": "Original", "type": "string"},
            "length": {"title": "Length", "type": "integer"},
            "as_list": {
                "title": "As List",
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }

    json_list_keys_tool = JsonListKeysTool(spec=JsonSpec(dict_={}))

    assert json_list_keys_tool.input_schema.schema() == {
        "title": "json_spec_list_keysSchema",
        "type": "object",
        "properties": {"tool_input": {"title": "Tool Input", "type": "string"}},
        "required": ["tool_input"],
    }
    assert json_list_keys_tool.output_schema.schema() == {
        "title": "JsonListKeysToolOutput"
    }


@pytest.mark.skipif(
    sys.version_info < (3, 9), reason="Requires python version >= 3.9 to run."
)
def test_lambda_schemas() -> None:
    first_lambda = lambda x: x["hello"]  # noqa: E731
    assert RunnableLambda(first_lambda).input_schema.schema() == {
        "title": "RunnableLambdaInput",
        "type": "object",
        "properties": {"hello": {"title": "Hello"}},
    }

    second_lambda = lambda x, y: (x["hello"], x["bye"], y["bah"])  # noqa: E731
    assert RunnableLambda(
        second_lambda,  # type: ignore[arg-type]
    ).input_schema.schema() == {
        "title": "RunnableLambdaInput",
        "type": "object",
        "properties": {"hello": {"title": "Hello"}, "bye": {"title": "Bye"}},
    }

    def get_value(input):  # type: ignore[no-untyped-def]
        return input["variable_name"]

    assert RunnableLambda(get_value).input_schema.schema() == {
        "title": "RunnableLambdaInput",
        "type": "object",
        "properties": {"variable_name": {"title": "Variable Name"}},
    }

    async def aget_value(input):  # type: ignore[no-untyped-def]
        return (input["variable_name"], input.get("another"))

    assert RunnableLambda(aget_value).input_schema.schema() == {
        "title": "RunnableLambdaInput",
        "type": "object",
        "properties": {
            "another": {"title": "Another"},
            "variable_name": {"title": "Variable Name"},
        },
    }

    async def aget_values(input):  # type: ignore[no-untyped-def]
        return {
            "hello": input["variable_name"],
            "bye": input["variable_name"],
            "byebye": input["yo"],
        }

    assert RunnableLambda(aget_values).input_schema.schema() == {
        "title": "RunnableLambdaInput",
        "type": "object",
        "properties": {
            "variable_name": {"title": "Variable Name"},
            "yo": {"title": "Yo"},
        },
    }


def test_schema_complex_seq() -> None:
    prompt1 = ChatPromptTemplate.from_template("what is the city {person} is from?")
    prompt2 = ChatPromptTemplate.from_template(
        "what country is the city {city} in? respond in {language}"
    )

    model = FakeListChatModel(responses=[""])

    chain1 = prompt1 | model | StrOutputParser()

    chain2: Runnable = (
        {"city": chain1, "language": itemgetter("language")}
        | prompt2
        | model
        | StrOutputParser()
    )

    assert chain2.input_schema.schema() == {
        "title": "RunnableMapInput",
        "type": "object",
        "properties": {
            "person": {"title": "Person"},
            "language": {"title": "Language"},
        },
    }

    assert chain2.output_schema.schema() == {
        "title": "StrOutputParserOutput",
        "type": "string",
    }


def test_schema_chains() -> None:
    model = FakeListChatModel(responses=[""])

    stuff_chain = load_summarize_chain(model)

    assert stuff_chain.input_schema.schema() == {
        "title": "CombineDocumentsInput",
        "type": "object",
        "properties": {
            "input_documents": {
                "title": "Input Documents",
                "type": "array",
                "items": {"$ref": "#/definitions/Document"},
            }
        },
        "definitions": {
            "Document": {
                "title": "Document",
                "description": "Class for storing a piece of text and associated metadata.",  # noqa: E501
                "type": "object",
                "properties": {
                    "page_content": {"title": "Page Content", "type": "string"},
                    "metadata": {"title": "Metadata", "type": "object"},
                },
                "required": ["page_content"],
            }
        },
    }
    assert stuff_chain.output_schema.schema() == {
        "title": "CombineDocumentsOutput",
        "type": "object",
        "properties": {"output_text": {"title": "Output Text", "type": "string"}},
    }

    mapreduce_chain = load_summarize_chain(
        model, "map_reduce", return_intermediate_steps=True
    )

    assert mapreduce_chain.input_schema.schema() == {
        "title": "CombineDocumentsInput",
        "type": "object",
        "properties": {
            "input_documents": {
                "title": "Input Documents",
                "type": "array",
                "items": {"$ref": "#/definitions/Document"},
            }
        },
        "definitions": {
            "Document": {
                "title": "Document",
                "description": "Class for storing a piece of text and associated metadata.",  # noqa: E501
                "type": "object",
                "properties": {
                    "page_content": {"title": "Page Content", "type": "string"},
                    "metadata": {"title": "Metadata", "type": "object"},
                },
                "required": ["page_content"],
            }
        },
    }
    assert mapreduce_chain.output_schema.schema() == {
        "title": "MapReduceDocumentsOutput",
        "type": "object",
        "properties": {
            "output_text": {"title": "Output Text", "type": "string"},
            "intermediate_steps": {
                "title": "Intermediate Steps",
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }

    maprerank_chain = load_qa_chain(model, "map_rerank", metadata_keys=["hello"])

    assert maprerank_chain.input_schema.schema() == {
        "title": "CombineDocumentsInput",
        "type": "object",
        "properties": {
            "input_documents": {
                "title": "Input Documents",
                "type": "array",
                "items": {"$ref": "#/definitions/Document"},
            }
        },
        "definitions": {
            "Document": {
                "title": "Document",
                "description": "Class for storing a piece of text and associated metadata.",  # noqa: E501
                "type": "object",
                "properties": {
                    "page_content": {"title": "Page Content", "type": "string"},
                    "metadata": {"title": "Metadata", "type": "object"},
                },
                "required": ["page_content"],
            }
        },
    }
    assert maprerank_chain.output_schema.schema() == {
        "title": "MapRerankOutput",
        "type": "object",
        "properties": {
            "output_text": {"title": "Output Text", "type": "string"},
            "hello": {"title": "Hello"},
        },
    }


@pytest.mark.asyncio
async def test_with_config(mocker: MockerFixture) -> None:
    fake = FakeRunnable()
    spy = mocker.spy(fake, "invoke")

    assert fake.with_config(tags=["a-tag"]).invoke("hello") == 5
    assert spy.call_args_list == [
        mocker.call("hello", dict(tags=["a-tag"])),
    ]
    spy.reset_mock()

    fake_1: Runnable = RunnablePassthrough()
    fake_2: Runnable = RunnablePassthrough()
    spy_seq_step = mocker.spy(fake_1.__class__, "invoke")

    sequence = fake_1.with_config(tags=["a-tag"]) | fake_2.with_config(
        tags=["b-tag"], max_concurrency=5
    )
    assert sequence.invoke("hello") == "hello"
    assert len(spy_seq_step.call_args_list) == 2
    for i, call in enumerate(spy_seq_step.call_args_list):
        assert call.args[1] == "hello"
        if i == 0:
            assert call.args[2].get("tags") == ["a-tag"]
            assert call.args[2].get("max_concurrency") is None
        else:
            assert call.args[2].get("tags") == ["b-tag"]
            assert call.args[2].get("max_concurrency") == 5
    mocker.stop(spy_seq_step)

    assert [
        *fake.with_config(tags=["a-tag"]).stream(
            "hello", dict(metadata={"key": "value"})
        )
    ] == [5]
    assert spy.call_args_list == [
        mocker.call("hello", dict(tags=["a-tag"], metadata={"key": "value"})),
    ]
    spy.reset_mock()

    assert fake.with_config(recursion_limit=5).batch(
        ["hello", "wooorld"], [dict(tags=["a-tag"]), dict(metadata={"key": "value"})]
    ) == [5, 7]

    assert len(spy.call_args_list) == 2
    for i, call in enumerate(spy.call_args_list):
        assert call.args[0] == ("hello" if i == 0 else "wooorld")
        if i == 0:
            assert call.args[1].get("recursion_limit") == 5
            assert call.args[1].get("tags") == ["a-tag"]
            assert call.args[1].get("metadata") == {}
        else:
            assert call.args[1].get("recursion_limit") == 5
            assert call.args[1].get("tags") == []
            assert call.args[1].get("metadata") == {"key": "value"}

    spy.reset_mock()

    assert fake.with_config(metadata={"a": "b"}).batch(
        ["hello", "wooorld"], dict(tags=["a-tag"])
    ) == [5, 7]
    assert len(spy.call_args_list) == 2
    for i, call in enumerate(spy.call_args_list):
        assert call.args[0] == ("hello" if i == 0 else "wooorld")
        assert call.args[1].get("tags") == ["a-tag"]
        assert call.args[1].get("metadata") == {"a": "b"}
    spy.reset_mock()

    handler = ConsoleCallbackHandler()
    assert (
        await fake.with_config(metadata={"a": "b"}).ainvoke(
            "hello", config={"callbacks": [handler]}
        )
        == 5
    )
    assert spy.call_args_list == [
        mocker.call("hello", dict(callbacks=[handler], metadata={"a": "b"})),
    ]
    spy.reset_mock()

    assert [
        part async for part in fake.with_config(metadata={"a": "b"}).astream("hello")
    ] == [5]
    assert spy.call_args_list == [
        mocker.call("hello", dict(metadata={"a": "b"})),
    ]
    spy.reset_mock()

    assert await fake.with_config(recursion_limit=5, tags=["c"]).abatch(
        ["hello", "wooorld"], dict(metadata={"key": "value"})
    ) == [
        5,
        7,
    ]
    assert spy.call_args_list == [
        mocker.call(
            "hello",
            dict(
                metadata={"key": "value"},
                tags=["c"],
                callbacks=None,
                locals={},
                recursion_limit=5,
            ),
        ),
        mocker.call(
            "wooorld",
            dict(
                metadata={"key": "value"},
                tags=["c"],
                callbacks=None,
                locals={},
                recursion_limit=5,
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_default_method_implementations(mocker: MockerFixture) -> None:
    fake = FakeRunnable()
    spy = mocker.spy(fake, "invoke")

    assert fake.invoke("hello", dict(tags=["a-tag"])) == 5
    assert spy.call_args_list == [
        mocker.call("hello", dict(tags=["a-tag"])),
    ]
    spy.reset_mock()

    assert [*fake.stream("hello", dict(metadata={"key": "value"}))] == [5]
    assert spy.call_args_list == [
        mocker.call("hello", dict(metadata={"key": "value"})),
    ]
    spy.reset_mock()

    assert fake.batch(
        ["hello", "wooorld"], [dict(tags=["a-tag"]), dict(metadata={"key": "value"})]
    ) == [5, 7]

    assert len(spy.call_args_list) == 2
    for i, call in enumerate(spy.call_args_list):
        assert call.args[0] == ("hello" if i == 0 else "wooorld")
        if i == 0:
            assert call.args[1].get("tags") == ["a-tag"]
            assert call.args[1].get("metadata") == {}
        else:
            assert call.args[1].get("tags") == []
            assert call.args[1].get("metadata") == {"key": "value"}

    spy.reset_mock()

    assert fake.batch(["hello", "wooorld"], dict(tags=["a-tag"])) == [5, 7]
    assert len(spy.call_args_list) == 2
    for i, call in enumerate(spy.call_args_list):
        assert call.args[0] == ("hello" if i == 0 else "wooorld")
        assert call.args[1].get("tags") == ["a-tag"]
        assert call.args[1].get("metadata") == {}
    spy.reset_mock()

    assert await fake.ainvoke("hello", config={"callbacks": []}) == 5
    assert spy.call_args_list == [
        mocker.call("hello", dict(callbacks=[])),
    ]
    spy.reset_mock()

    assert [part async for part in fake.astream("hello")] == [5]
    assert spy.call_args_list == [
        mocker.call("hello", None),
    ]
    spy.reset_mock()

    assert await fake.abatch(["hello", "wooorld"], dict(metadata={"key": "value"})) == [
        5,
        7,
    ]
    assert spy.call_args_list == [
        mocker.call(
            "hello",
            dict(
                metadata={"key": "value"},
                tags=[],
                callbacks=None,
                locals={},
                recursion_limit=10,
            ),
        ),
        mocker.call(
            "wooorld",
            dict(
                metadata={"key": "value"},
                tags=[],
                callbacks=None,
                locals={},
                recursion_limit=10,
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_prompt() -> None:
    prompt = ChatPromptTemplate.from_messages(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessagePromptTemplate.from_template("{question}"),
        ]
    )
    expected = ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )

    assert prompt.invoke({"question": "What is your name?"}) == expected

    assert prompt.batch(
        [
            {"question": "What is your name?"},
            {"question": "What is your favorite color?"},
        ]
    ) == [
        expected,
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your favorite color?"),
            ]
        ),
    ]

    assert [*prompt.stream({"question": "What is your name?"})] == [expected]

    assert await prompt.ainvoke({"question": "What is your name?"}) == expected

    assert await prompt.abatch(
        [
            {"question": "What is your name?"},
            {"question": "What is your favorite color?"},
        ]
    ) == [
        expected,
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your favorite color?"),
            ]
        ),
    ]

    assert [
        part async for part in prompt.astream({"question": "What is your name?"})
    ] == [expected]

    stream_log = [
        part async for part in prompt.astream_log({"question": "What is your name?"})
    ]

    assert len(stream_log[0].ops) == 1
    assert stream_log[0].ops[0]["op"] == "replace"
    assert stream_log[0].ops[0]["path"] == ""
    assert stream_log[0].ops[0]["value"]["logs"] == []
    assert stream_log[0].ops[0]["value"]["final_output"] is None
    assert stream_log[0].ops[0]["value"]["streamed_output"] == []
    assert type(stream_log[0].ops[0]["value"]["id"]) == str

    assert stream_log[1:] == [
        RunLogPatch(
            {
                "op": "replace",
                "path": "/final_output",
                "value": {
                    "id": ["langchain", "prompts", "chat", "ChatPromptValue"],
                    "kwargs": {
                        "messages": [
                            {
                                "id": [
                                    "langchain",
                                    "schema",
                                    "messages",
                                    "SystemMessage",
                                ],
                                "kwargs": {"content": "You are a nice " "assistant."},
                                "lc": 1,
                                "type": "constructor",
                            },
                            {
                                "id": [
                                    "langchain",
                                    "schema",
                                    "messages",
                                    "HumanMessage",
                                ],
                                "kwargs": {
                                    "additional_kwargs": {},
                                    "content": "What is your " "name?",
                                },
                                "lc": 1,
                                "type": "constructor",
                            },
                        ]
                    },
                    "lc": 1,
                    "type": "constructor",
                },
            }
        ),
        RunLogPatch({"op": "add", "path": "/streamed_output/-", "value": expected}),
    ]


def test_prompt_template_params() -> None:
    prompt = ChatPromptTemplate.from_template(
        "Respond to the following question: {question}"
    )
    result = prompt.invoke(
        {
            "question": "test",
            "topic": "test",
        }
    )
    assert result == ChatPromptValue(
        messages=[HumanMessage(content="Respond to the following question: test")]
    )

    with pytest.raises(KeyError):
        prompt.invoke({})


@pytest.mark.asyncio
@freeze_time("2023-01-01")
async def test_prompt_with_chat_model(
    mocker: MockerFixture, snapshot: SnapshotAssertion
) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    chat = FakeListChatModel(responses=["foo"])

    chain = prompt | chat

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == []
    assert chain.last == chat
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == AIMessage(content="foo")
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )

    assert tracer.runs == snapshot

    mocker.stop(prompt_spy)
    mocker.stop(chat_spy)

    # Test batch
    prompt_spy = mocker.spy(prompt.__class__, "batch")
    chat_spy = mocker.spy(chat.__class__, "batch")
    tracer = FakeTracer()
    assert chain.batch(
        [
            {"question": "What is your name?"},
            {"question": "What is your favorite color?"},
        ],
        dict(callbacks=[tracer]),
    ) == [
        AIMessage(content="foo"),
        AIMessage(content="foo"),
    ]
    assert prompt_spy.call_args.args[1] == [
        {"question": "What is your name?"},
        {"question": "What is your favorite color?"},
    ]
    assert chat_spy.call_args.args[1] == [
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your name?"),
            ]
        ),
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your favorite color?"),
            ]
        ),
    ]
    assert (
        len(
            [
                r
                for r in tracer.runs
                if r.parent_run_id is None and len(r.child_runs) == 2
            ]
        )
        == 2
    ), "Each of 2 outer runs contains exactly two inner runs (1 prompt, 1 chat)"
    mocker.stop(prompt_spy)
    mocker.stop(chat_spy)

    # Test stream
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "stream")
    tracer = FakeTracer()
    assert [
        *chain.stream({"question": "What is your name?"}, dict(callbacks=[tracer]))
    ] == [AIMessage(content="f"), AIMessage(content="o"), AIMessage(content="o")]
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )


@pytest.mark.asyncio
@freeze_time("2023-01-01")
async def test_prompt_with_llm(
    mocker: MockerFixture, snapshot: SnapshotAssertion
) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    llm = FakeListLLM(responses=["foo", "bar"])

    chain = prompt | llm

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == []
    assert chain.last == llm
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "ainvoke")
    llm_spy = mocker.spy(llm.__class__, "ainvoke")
    tracer = FakeTracer()
    assert (
        await chain.ainvoke(
            {"question": "What is your name?"}, dict(callbacks=[tracer])
        )
        == "foo"
    )
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert tracer.runs == snapshot
    mocker.stop(prompt_spy)
    mocker.stop(llm_spy)

    # Test batch
    prompt_spy = mocker.spy(prompt.__class__, "abatch")
    llm_spy = mocker.spy(llm.__class__, "abatch")
    tracer = FakeTracer()
    assert await chain.abatch(
        [
            {"question": "What is your name?"},
            {"question": "What is your favorite color?"},
        ],
        dict(callbacks=[tracer]),
    ) == ["bar", "foo"]
    assert prompt_spy.call_args.args[1] == [
        {"question": "What is your name?"},
        {"question": "What is your favorite color?"},
    ]
    assert llm_spy.call_args.args[1] == [
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your name?"),
            ]
        ),
        ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your favorite color?"),
            ]
        ),
    ]
    assert tracer.runs == snapshot
    mocker.stop(prompt_spy)
    mocker.stop(llm_spy)

    # Test stream
    prompt_spy = mocker.spy(prompt.__class__, "ainvoke")
    llm_spy = mocker.spy(llm.__class__, "astream")
    tracer = FakeTracer()
    assert [
        token
        async for token in chain.astream(
            {"question": "What is your name?"}, dict(callbacks=[tracer])
        )
    ] == ["bar"]
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )

    prompt_spy.reset_mock()
    llm_spy.reset_mock()
    stream_log = [
        part async for part in chain.astream_log({"question": "What is your name?"})
    ]

    # remove ids from logs
    for part in stream_log:
        for op in part.ops:
            if (
                isinstance(op["value"], dict)
                and "id" in op["value"]
                and not isinstance(op["value"]["id"], list)  # serialized lc id
            ):
                del op["value"]["id"]

    assert stream_log == [
        RunLogPatch(
            {
                "op": "replace",
                "path": "",
                "value": {
                    "logs": [],
                    "final_output": None,
                    "streamed_output": [],
                },
            }
        ),
        RunLogPatch(
            {
                "op": "add",
                "path": "/logs/0",
                "value": {
                    "end_time": None,
                    "final_output": None,
                    "metadata": {},
                    "name": "ChatPromptTemplate",
                    "start_time": "2023-01-01T00:00:00.000",
                    "streamed_output_str": [],
                    "tags": ["seq:step:1"],
                    "type": "prompt",
                },
            }
        ),
        RunLogPatch(
            {
                "op": "add",
                "path": "/logs/0/final_output",
                "value": {
                    "id": ["langchain", "prompts", "chat", "ChatPromptValue"],
                    "kwargs": {
                        "messages": [
                            {
                                "id": [
                                    "langchain",
                                    "schema",
                                    "messages",
                                    "SystemMessage",
                                ],
                                "kwargs": {
                                    "additional_kwargs": {},
                                    "content": "You are a nice " "assistant.",
                                },
                                "lc": 1,
                                "type": "constructor",
                            },
                            {
                                "id": [
                                    "langchain",
                                    "schema",
                                    "messages",
                                    "HumanMessage",
                                ],
                                "kwargs": {
                                    "additional_kwargs": {},
                                    "content": "What is your " "name?",
                                },
                                "lc": 1,
                                "type": "constructor",
                            },
                        ]
                    },
                    "lc": 1,
                    "type": "constructor",
                },
            },
            {
                "op": "add",
                "path": "/logs/0/end_time",
                "value": "2023-01-01T00:00:00.000",
            },
        ),
        RunLogPatch(
            {
                "op": "add",
                "path": "/logs/1",
                "value": {
                    "end_time": None,
                    "final_output": None,
                    "metadata": {},
                    "name": "FakeListLLM",
                    "start_time": "2023-01-01T00:00:00.000",
                    "streamed_output_str": [],
                    "tags": ["seq:step:2"],
                    "type": "llm",
                },
            }
        ),
        RunLogPatch(
            {
                "op": "add",
                "path": "/logs/1/final_output",
                "value": {
                    "generations": [[{"generation_info": None, "text": "foo"}]],
                    "llm_output": None,
                    "run": None,
                },
            },
            {
                "op": "add",
                "path": "/logs/1/end_time",
                "value": "2023-01-01T00:00:00.000",
            },
        ),
        RunLogPatch({"op": "add", "path": "/streamed_output/-", "value": "foo"}),
        RunLogPatch(
            {"op": "replace", "path": "/final_output", "value": {"output": "foo"}}
        ),
    ]


@pytest.mark.asyncio
@freeze_time("2023-01-01")
async def test_prompt_with_llm_and_async_lambda(
    mocker: MockerFixture, snapshot: SnapshotAssertion
) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    llm = FakeListLLM(responses=["foo", "bar"])

    async def passthrough(input: Any) -> Any:
        return input

    chain = prompt | llm | passthrough

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == [llm]
    assert chain.last == RunnableLambda(func=passthrough)
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "ainvoke")
    llm_spy = mocker.spy(llm.__class__, "ainvoke")
    tracer = FakeTracer()
    assert (
        await chain.ainvoke(
            {"question": "What is your name?"}, dict(callbacks=[tracer])
        )
        == "foo"
    )
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert tracer.runs == snapshot
    mocker.stop(prompt_spy)
    mocker.stop(llm_spy)


@freeze_time("2023-01-01")
def test_prompt_with_chat_model_and_parser(
    mocker: MockerFixture, snapshot: SnapshotAssertion
) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    chat = FakeListChatModel(responses=["foo, bar"])
    parser = CommaSeparatedListOutputParser()

    chain = prompt | chat | parser

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == [chat]
    assert chain.last == parser
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    parser_spy = mocker.spy(parser.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == ["foo", "bar"]
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert parser_spy.call_args.args[1] == AIMessage(content="foo, bar")

    assert tracer.runs == snapshot


@freeze_time("2023-01-01")
def test_combining_sequences(
    mocker: MockerFixture, snapshot: SnapshotAssertion
) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    chat = FakeListChatModel(responses=["foo, bar"])
    parser = CommaSeparatedListOutputParser()

    chain = prompt | chat | parser

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == [chat]
    assert chain.last == parser
    assert dumps(chain, pretty=True) == snapshot

    prompt2 = (
        SystemMessagePromptTemplate.from_template("You are a nicer assistant.")
        + "{question}"
    )
    chat2 = FakeListChatModel(responses=["baz, qux"])
    parser2 = CommaSeparatedListOutputParser()
    input_formatter: RunnableLambda[List[str], Dict[str, Any]] = RunnableLambda(
        lambda x: {"question": x[0] + x[1]}
    )

    chain2 = input_formatter | prompt2 | chat2 | parser2

    assert isinstance(chain, RunnableSequence)
    assert chain2.first == input_formatter
    assert chain2.middle == [prompt2, chat2]
    assert chain2.last == parser2
    assert dumps(chain2, pretty=True) == snapshot

    combined_chain = chain | chain2

    assert combined_chain.first == prompt
    assert combined_chain.middle == [
        chat,
        parser,
        input_formatter,
        prompt2,
        chat2,
    ]
    assert combined_chain.last == parser2
    assert dumps(combined_chain, pretty=True) == snapshot

    # Test invoke
    tracer = FakeTracer()
    assert combined_chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == ["baz", "qux"]

    assert tracer.runs == snapshot


@freeze_time("2023-01-01")
def test_seq_dict_prompt_llm(
    mocker: MockerFixture, snapshot: SnapshotAssertion
) -> None:
    passthrough = mocker.Mock(side_effect=lambda x: x)

    retriever = FakeRetriever()

    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + """Context:
{documents}

Question:
{question}"""
    )

    chat = FakeListChatModel(responses=["foo, bar"])

    parser = CommaSeparatedListOutputParser()

    chain: Runnable = (
        {
            "question": RunnablePassthrough[str]() | passthrough,
            "documents": passthrough | retriever,
            "just_to_test_lambda": passthrough,
        }
        | prompt
        | chat
        | parser
    )

    assert isinstance(chain, RunnableSequence)
    assert isinstance(chain.first, RunnableMap)
    assert chain.middle == [prompt, chat]
    assert chain.last == parser
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    parser_spy = mocker.spy(parser.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke("What is your name?", dict(callbacks=[tracer])) == [
        "foo",
        "bar",
    ]
    assert prompt_spy.call_args.args[1] == {
        "documents": [Document(page_content="foo"), Document(page_content="bar")],
        "question": "What is your name?",
        "just_to_test_lambda": "What is your name?",
    }
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(
                content="""Context:
[Document(page_content='foo', metadata={}), Document(page_content='bar', metadata={})]

Question:
What is your name?"""
            ),
        ]
    )
    assert parser_spy.call_args.args[1] == AIMessage(content="foo, bar")
    assert len([r for r in tracer.runs if r.parent_run_id is None]) == 1
    parent_run = next(r for r in tracer.runs if r.parent_run_id is None)
    assert len(parent_run.child_runs) == 4
    map_run = parent_run.child_runs[0]
    assert map_run.name == "RunnableMap"
    assert len(map_run.child_runs) == 3


@freeze_time("2023-01-01")
def test_seq_prompt_dict(mocker: MockerFixture, snapshot: SnapshotAssertion) -> None:
    passthrough = mocker.Mock(side_effect=lambda x: x)

    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )

    chat = FakeListChatModel(responses=["i'm a chatbot"])

    llm = FakeListLLM(responses=["i'm a textbot"])

    chain = (
        prompt
        | passthrough
        | {
            "chat": chat,
            "llm": llm,
        }
    )

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == [RunnableLambda(passthrough)]
    assert isinstance(chain.last, RunnableMap)
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    llm_spy = mocker.spy(llm.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == {
        "chat": AIMessage(content="i'm a chatbot"),
        "llm": "i'm a textbot",
    }
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert len([r for r in tracer.runs if r.parent_run_id is None]) == 1
    parent_run = next(r for r in tracer.runs if r.parent_run_id is None)
    assert len(parent_run.child_runs) == 3
    map_run = parent_run.child_runs[2]
    assert map_run.name == "RunnableMap"
    assert len(map_run.child_runs) == 2


@pytest.mark.asyncio
@freeze_time("2023-01-01")
async def test_router_runnable(
    mocker: MockerFixture, snapshot: SnapshotAssertion
) -> None:
    chain1 = ChatPromptTemplate.from_template(
        "You are a math genius. Answer the question: {question}"
    ) | FakeListLLM(responses=["4"])
    chain2 = ChatPromptTemplate.from_template(
        "You are an english major. Answer the question: {question}"
    ) | FakeListLLM(responses=["2"])
    router = RouterRunnable({"math": chain1, "english": chain2})
    chain: Runnable = {
        "key": lambda x: x["key"],
        "input": {"question": lambda x: x["question"]},
    } | router
    assert dumps(chain, pretty=True) == snapshot

    result = chain.invoke({"key": "math", "question": "2 + 2"})
    assert result == "4"

    result2 = chain.batch(
        [{"key": "math", "question": "2 + 2"}, {"key": "english", "question": "2 + 2"}]
    )
    assert result2 == ["4", "2"]

    result = await chain.ainvoke({"key": "math", "question": "2 + 2"})
    assert result == "4"

    result2 = await chain.abatch(
        [{"key": "math", "question": "2 + 2"}, {"key": "english", "question": "2 + 2"}]
    )
    assert result2 == ["4", "2"]

    # Test invoke
    router_spy = mocker.spy(router.__class__, "invoke")
    tracer = FakeTracer()
    assert (
        chain.invoke({"key": "math", "question": "2 + 2"}, dict(callbacks=[tracer]))
        == "4"
    )
    assert router_spy.call_args.args[1] == {
        "key": "math",
        "input": {"question": "2 + 2"},
    }
    assert len([r for r in tracer.runs if r.parent_run_id is None]) == 1
    parent_run = next(r for r in tracer.runs if r.parent_run_id is None)
    assert len(parent_run.child_runs) == 2
    router_run = parent_run.child_runs[1]
    assert router_run.name == "RunnableSequence"  # TODO: should be RunnableRouter
    assert len(router_run.child_runs) == 2


@pytest.mark.asyncio
@freeze_time("2023-01-01")
async def test_higher_order_lambda_runnable(
    mocker: MockerFixture, snapshot: SnapshotAssertion
) -> None:
    math_chain = ChatPromptTemplate.from_template(
        "You are a math genius. Answer the question: {question}"
    ) | FakeListLLM(responses=["4"])
    english_chain = ChatPromptTemplate.from_template(
        "You are an english major. Answer the question: {question}"
    ) | FakeListLLM(responses=["2"])
    input_map: Runnable = RunnableMap(
        {  # type: ignore[arg-type]
            "key": lambda x: x["key"],
            "input": {"question": lambda x: x["question"]},
        }
    )

    def router(input: Dict[str, Any]) -> Runnable:
        if input["key"] == "math":
            return itemgetter("input") | math_chain
        elif input["key"] == "english":
            return itemgetter("input") | english_chain
        else:
            raise ValueError(f"Unknown key: {input['key']}")

    chain: Runnable = input_map | router
    assert dumps(chain, pretty=True) == snapshot

    result = chain.invoke({"key": "math", "question": "2 + 2"})
    assert result == "4"

    result2 = chain.batch(
        [{"key": "math", "question": "2 + 2"}, {"key": "english", "question": "2 + 2"}]
    )
    assert result2 == ["4", "2"]

    result = await chain.ainvoke({"key": "math", "question": "2 + 2"})
    assert result == "4"

    result2 = await chain.abatch(
        [{"key": "math", "question": "2 + 2"}, {"key": "english", "question": "2 + 2"}]
    )
    assert result2 == ["4", "2"]

    # Test invoke
    math_spy = mocker.spy(math_chain.__class__, "invoke")
    tracer = FakeTracer()
    assert (
        chain.invoke({"key": "math", "question": "2 + 2"}, dict(callbacks=[tracer]))
        == "4"
    )
    assert math_spy.call_args.args[1] == {
        "key": "math",
        "input": {"question": "2 + 2"},
    }
    assert len([r for r in tracer.runs if r.parent_run_id is None]) == 1
    parent_run = next(r for r in tracer.runs if r.parent_run_id is None)
    assert len(parent_run.child_runs) == 2
    router_run = parent_run.child_runs[1]
    assert router_run.name == "router"
    assert len(router_run.child_runs) == 1
    math_run = router_run.child_runs[0]
    assert math_run.name == "RunnableSequence"
    assert len(math_run.child_runs) == 3

    # Test ainvoke
    async def arouter(input: Dict[str, Any]) -> Runnable:
        if input["key"] == "math":
            return itemgetter("input") | math_chain
        elif input["key"] == "english":
            return itemgetter("input") | english_chain
        else:
            raise ValueError(f"Unknown key: {input['key']}")

    achain: Runnable = input_map | arouter
    math_spy = mocker.spy(math_chain.__class__, "ainvoke")
    tracer = FakeTracer()
    assert (
        await achain.ainvoke(
            {"key": "math", "question": "2 + 2"}, dict(callbacks=[tracer])
        )
        == "4"
    )
    assert math_spy.call_args.args[1] == {
        "key": "math",
        "input": {"question": "2 + 2"},
    }
    assert len([r for r in tracer.runs if r.parent_run_id is None]) == 1
    parent_run = next(r for r in tracer.runs if r.parent_run_id is None)
    assert len(parent_run.child_runs) == 2
    router_run = parent_run.child_runs[1]
    assert router_run.name == "arouter"
    assert len(router_run.child_runs) == 1
    math_run = router_run.child_runs[0]
    assert math_run.name == "RunnableSequence"
    assert len(math_run.child_runs) == 3


@freeze_time("2023-01-01")
def test_seq_prompt_map(mocker: MockerFixture, snapshot: SnapshotAssertion) -> None:
    passthrough = mocker.Mock(side_effect=lambda x: x)

    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )

    chat = FakeListChatModel(responses=["i'm a chatbot"])

    llm = FakeListLLM(responses=["i'm a textbot"])

    chain = (
        prompt
        | passthrough
        | {
            "chat": chat.bind(stop=["Thought:"]),
            "llm": llm,
            "passthrough": passthrough,
        }
    )

    assert isinstance(chain, RunnableSequence)
    assert chain.first == prompt
    assert chain.middle == [RunnableLambda(passthrough)]
    assert isinstance(chain.last, RunnableMap)
    assert dumps(chain, pretty=True) == snapshot

    # Test invoke
    prompt_spy = mocker.spy(prompt.__class__, "invoke")
    chat_spy = mocker.spy(chat.__class__, "invoke")
    llm_spy = mocker.spy(llm.__class__, "invoke")
    tracer = FakeTracer()
    assert chain.invoke(
        {"question": "What is your name?"}, dict(callbacks=[tracer])
    ) == {
        "chat": AIMessage(content="i'm a chatbot"),
        "llm": "i'm a textbot",
        "passthrough": ChatPromptValue(
            messages=[
                SystemMessage(content="You are a nice assistant."),
                HumanMessage(content="What is your name?"),
            ]
        ),
    }
    assert prompt_spy.call_args.args[1] == {"question": "What is your name?"}
    assert chat_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert llm_spy.call_args.args[1] == ChatPromptValue(
        messages=[
            SystemMessage(content="You are a nice assistant."),
            HumanMessage(content="What is your name?"),
        ]
    )
    assert len([r for r in tracer.runs if r.parent_run_id is None]) == 1
    parent_run = next(r for r in tracer.runs if r.parent_run_id is None)
    assert len(parent_run.child_runs) == 3
    map_run = parent_run.child_runs[2]
    assert map_run.name == "RunnableMap"
    assert len(map_run.child_runs) == 3


def test_map_stream() -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )

    chat_res = "i'm a chatbot"
    # sleep to better simulate a real stream
    chat = FakeListChatModel(responses=[chat_res], sleep=0.01)

    llm_res = "i'm a textbot"
    # sleep to better simulate a real stream
    llm = FakeStreamingListLLM(responses=[llm_res], sleep=0.01)

    chain: Runnable = prompt | {
        "chat": chat.bind(stop=["Thought:"]),
        "llm": llm,
        "passthrough": RunnablePassthrough(),
    }

    stream = chain.stream({"question": "What is your name?"})

    final_value = None
    streamed_chunks = []
    for chunk in stream:
        streamed_chunks.append(chunk)
        if final_value is None:
            final_value = chunk
        else:
            final_value += chunk

    assert streamed_chunks[0] in [
        {"passthrough": prompt.invoke({"question": "What is your name?"})},
        {"llm": "i"},
        {"chat": AIMessageChunk(content="i")},
    ]
    assert len(streamed_chunks) == len(chat_res) + len(llm_res) + 1
    assert all(len(c.keys()) == 1 for c in streamed_chunks)
    assert final_value is not None
    assert final_value.get("chat").content == "i'm a chatbot"
    assert final_value.get("llm") == "i'm a textbot"
    assert final_value.get("passthrough") == prompt.invoke(
        {"question": "What is your name?"}
    )


def test_map_stream_iterator_input() -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )

    chat_res = "i'm a chatbot"
    # sleep to better simulate a real stream
    chat = FakeListChatModel(responses=[chat_res], sleep=0.01)

    llm_res = "i'm a textbot"
    # sleep to better simulate a real stream
    llm = FakeStreamingListLLM(responses=[llm_res], sleep=0.01)

    chain: Runnable = (
        prompt
        | llm
        | {
            "chat": chat.bind(stop=["Thought:"]),
            "llm": llm,
            "passthrough": RunnablePassthrough(),
        }
    )

    stream = chain.stream({"question": "What is your name?"})

    final_value = None
    streamed_chunks = []
    for chunk in stream:
        streamed_chunks.append(chunk)
        if final_value is None:
            final_value = chunk
        else:
            final_value += chunk

    assert streamed_chunks[0] in [
        {"passthrough": "i"},
        {"llm": "i"},
        {"chat": AIMessageChunk(content="i")},
    ]
    assert len(streamed_chunks) == len(chat_res) + len(llm_res) + len(llm_res)
    assert all(len(c.keys()) == 1 for c in streamed_chunks)
    assert final_value is not None
    assert final_value.get("chat").content == "i'm a chatbot"
    assert final_value.get("llm") == "i'm a textbot"
    assert final_value.get("passthrough") == "i'm a textbot"


@pytest.mark.asyncio
async def test_map_astream() -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )

    chat_res = "i'm a chatbot"
    # sleep to better simulate a real stream
    chat = FakeListChatModel(responses=[chat_res], sleep=0.01)

    llm_res = "i'm a textbot"
    # sleep to better simulate a real stream
    llm = FakeStreamingListLLM(responses=[llm_res], sleep=0.01)

    chain: Runnable = prompt | {
        "chat": chat.bind(stop=["Thought:"]),
        "llm": llm,
        "passthrough": RunnablePassthrough(),
    }

    stream = chain.astream({"question": "What is your name?"})

    final_value = None
    streamed_chunks = []
    async for chunk in stream:
        streamed_chunks.append(chunk)
        if final_value is None:
            final_value = chunk
        else:
            final_value += chunk

    assert streamed_chunks[0] in [
        {"passthrough": prompt.invoke({"question": "What is your name?"})},
        {"llm": "i"},
        {"chat": AIMessageChunk(content="i")},
    ]
    assert len(streamed_chunks) == len(chat_res) + len(llm_res) + 1
    assert all(len(c.keys()) == 1 for c in streamed_chunks)
    assert final_value is not None
    assert final_value.get("chat").content == "i'm a chatbot"
    assert final_value.get("llm") == "i'm a textbot"
    assert final_value.get("passthrough") == prompt.invoke(
        {"question": "What is your name?"}
    )

    # Test astream_log state accumulation

    final_state = None
    streamed_ops = []
    async for chunk in chain.astream_log({"question": "What is your name?"}):
        streamed_ops.extend(chunk.ops)
        if final_state is None:
            final_state = chunk
        else:
            final_state += chunk
    final_state = cast(RunLog, final_state)

    assert final_state.state["final_output"] == final_value
    assert len(final_state.state["streamed_output"]) == len(streamed_chunks)
    assert type(final_state.state["id"]) == str
    assert len(final_state.ops) == len(streamed_ops)
    assert len(final_state.state["logs"]) == 5
    assert final_state.state["logs"][0]["name"] == "ChatPromptTemplate"
    assert final_state.state["logs"][0]["final_output"] == dumpd(
        prompt.invoke({"question": "What is your name?"})
    )
    assert final_state.state["logs"][1]["name"] == "RunnableMap"
    assert sorted(log["name"] for log in final_state.state["logs"][2:]) == [
        "FakeListChatModel",
        "FakeStreamingListLLM",
        "RunnablePassthrough",
    ]

    # Test astream_log with include filters
    final_state = None
    async for chunk in chain.astream_log(
        {"question": "What is your name?"}, include_names=["FakeListChatModel"]
    ):
        if final_state is None:
            final_state = chunk
        else:
            final_state += chunk
    final_state = cast(RunLog, final_state)

    assert final_state.state["final_output"] == final_value
    assert len(final_state.state["streamed_output"]) == len(streamed_chunks)
    assert len(final_state.state["logs"]) == 1
    assert final_state.state["logs"][0]["name"] == "FakeListChatModel"

    # Test astream_log with exclude filters
    final_state = None
    async for chunk in chain.astream_log(
        {"question": "What is your name?"}, exclude_names=["FakeListChatModel"]
    ):
        if final_state is None:
            final_state = chunk
        else:
            final_state += chunk
    final_state = cast(RunLog, final_state)

    assert final_state.state["final_output"] == final_value
    assert len(final_state.state["streamed_output"]) == len(streamed_chunks)
    assert len(final_state.state["logs"]) == 4
    assert final_state.state["logs"][0]["name"] == "ChatPromptTemplate"
    assert final_state.state["logs"][0]["final_output"] == dumpd(
        prompt.invoke({"question": "What is your name?"})
    )
    assert final_state.state["logs"][1]["name"] == "RunnableMap"
    assert sorted(log["name"] for log in final_state.state["logs"][2:]) == [
        "FakeStreamingListLLM",
        "RunnablePassthrough",
    ]


@pytest.mark.asyncio
async def test_map_astream_iterator_input() -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )

    chat_res = "i'm a chatbot"
    # sleep to better simulate a real stream
    chat = FakeListChatModel(responses=[chat_res], sleep=0.01)

    llm_res = "i'm a textbot"
    # sleep to better simulate a real stream
    llm = FakeStreamingListLLM(responses=[llm_res], sleep=0.01)

    chain: Runnable = (
        prompt
        | llm
        | {
            "chat": chat.bind(stop=["Thought:"]),
            "llm": llm,
            "passthrough": RunnablePassthrough(),
        }
    )

    stream = chain.astream({"question": "What is your name?"})

    final_value = None
    streamed_chunks = []
    async for chunk in stream:
        streamed_chunks.append(chunk)
        if final_value is None:
            final_value = chunk
        else:
            final_value += chunk

    assert streamed_chunks[0] in [
        {"passthrough": "i"},
        {"llm": "i"},
        {"chat": AIMessageChunk(content="i")},
    ]
    assert len(streamed_chunks) == len(chat_res) + len(llm_res) + len(llm_res)
    assert all(len(c.keys()) == 1 for c in streamed_chunks)
    assert final_value is not None
    assert final_value.get("chat").content == "i'm a chatbot"
    assert final_value.get("llm") == "i'm a textbot"
    assert final_value.get("passthrough") == llm_res


def test_with_config_with_config() -> None:
    llm = FakeListLLM(responses=["i'm a textbot"])

    assert dumpd(
        llm.with_config({"metadata": {"a": "b"}}).with_config(tags=["a-tag"])
    ) == dumpd(llm.with_config({"metadata": {"a": "b"}, "tags": ["a-tag"]}))


def test_metadata_is_merged() -> None:
    """Test metadata and tags defined in with_config and at are merged/concatend."""

    foo = RunnableLambda(lambda x: x).with_config({"metadata": {"my_key": "my_value"}})
    expected_metadata = {
        "my_key": "my_value",
        "my_other_key": "my_other_value",
    }
    with collect_runs() as cb:
        foo.invoke("hi", {"metadata": {"my_other_key": "my_other_value"}})
        run = cb.traced_runs[0]
    assert run.extra["metadata"] == expected_metadata


def test_tags_are_appended() -> None:
    """Test tags from with_config are concatenated with those in invocation."""

    foo = RunnableLambda(lambda x: x).with_config({"tags": ["my_key"]})
    with collect_runs() as cb:
        foo.invoke("hi", {"tags": ["invoked_key"]})
        run = cb.traced_runs[0]
    assert isinstance(run.tags, list)
    assert sorted(run.tags) == sorted(["my_key", "invoked_key"])


def test_bind_bind() -> None:
    llm = FakeListLLM(responses=["i'm a textbot"])

    assert dumpd(
        llm.bind(stop=["Thought:"], one="two").bind(
            stop=["Observation:"], hello="world"
        )
    ) == dumpd(llm.bind(stop=["Observation:"], one="two", hello="world"))


def test_deep_stream() -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    llm = FakeStreamingListLLM(responses=["foo-lish"])

    chain = prompt | llm | StrOutputParser()

    stream = chain.stream({"question": "What up"})

    chunks = []
    for chunk in stream:
        chunks.append(chunk)

    assert len(chunks) == len("foo-lish")
    assert "".join(chunks) == "foo-lish"

    chunks = []
    for chunk in (chain | RunnablePassthrough()).stream({"question": "What up"}):
        chunks.append(chunk)

    assert len(chunks) == len("foo-lish")
    assert "".join(chunks) == "foo-lish"


@pytest.mark.asyncio
async def test_deep_astream() -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    llm = FakeStreamingListLLM(responses=["foo-lish"])

    chain = prompt | llm | StrOutputParser()

    stream = chain.astream({"question": "What up"})

    chunks = []
    async for chunk in stream:
        chunks.append(chunk)

    assert len(chunks) == len("foo-lish")
    assert "".join(chunks) == "foo-lish"

    chunks = []
    async for chunk in (chain | RunnablePassthrough()).astream({"question": "What up"}):
        chunks.append(chunk)

    assert len(chunks) == len("foo-lish")
    assert "".join(chunks) == "foo-lish"


def test_runnable_sequence_transform() -> None:
    llm = FakeStreamingListLLM(responses=["foo-lish"])

    chain = llm | StrOutputParser()

    stream = chain.transform(llm.stream("Hi there!"))

    chunks = []
    for chunk in stream:
        chunks.append(chunk)

    assert len(chunks) == len("foo-lish")
    assert "".join(chunks) == "foo-lish"


@pytest.mark.asyncio
async def test_runnable_sequence_atransform() -> None:
    llm = FakeStreamingListLLM(responses=["foo-lish"])

    chain = llm | StrOutputParser()

    stream = chain.atransform(llm.astream("Hi there!"))

    chunks = []
    async for chunk in stream:
        chunks.append(chunk)

    assert len(chunks) == len("foo-lish")
    assert "".join(chunks) == "foo-lish"


@pytest.fixture()
def llm_with_fallbacks() -> RunnableWithFallbacks:
    error_llm = FakeListLLM(responses=["foo"], i=1)
    pass_llm = FakeListLLM(responses=["bar"])

    return error_llm.with_fallbacks([pass_llm])


@pytest.fixture()
def llm_with_multi_fallbacks() -> RunnableWithFallbacks:
    error_llm = FakeListLLM(responses=["foo"], i=1)
    error_llm_2 = FakeListLLM(responses=["baz"], i=1)
    pass_llm = FakeListLLM(responses=["bar"])

    return error_llm.with_fallbacks([error_llm_2, pass_llm])


@pytest.fixture()
def llm_chain_with_fallbacks() -> RunnableSequence:
    error_llm = FakeListLLM(responses=["foo"], i=1)
    pass_llm = FakeListLLM(responses=["bar"])

    prompt = PromptTemplate.from_template("what did baz say to {buz}")
    return RunnableMap({"buz": lambda x: x}) | (prompt | error_llm).with_fallbacks(
        [prompt | pass_llm]
    )


@pytest.mark.parametrize(
    "runnable",
    ["llm_with_fallbacks", "llm_with_multi_fallbacks", "llm_chain_with_fallbacks"],
)
@pytest.mark.asyncio
async def test_llm_with_fallbacks(
    runnable: RunnableWithFallbacks, request: Any, snapshot: SnapshotAssertion
) -> None:
    runnable = request.getfixturevalue(runnable)
    assert runnable.invoke("hello") == "bar"
    assert runnable.batch(["hi", "hey", "bye"]) == ["bar"] * 3
    assert list(runnable.stream("hello")) == ["bar"]
    assert await runnable.ainvoke("hello") == "bar"
    assert await runnable.abatch(["hi", "hey", "bye"]) == ["bar"] * 3
    assert list(await runnable.ainvoke("hello")) == list("bar")
    assert dumps(runnable, pretty=True) == snapshot


class FakeSplitIntoListParser(BaseOutputParser[List[str]]):
    """Parse the output of an LLM call to a comma-separated list."""

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """Return whether or not the class is serializable."""
        return True

    def get_format_instructions(self) -> str:
        return (
            "Your response should be a list of comma separated values, "
            "eg: `foo, bar, baz`"
        )

    def parse(self, text: str) -> List[str]:
        """Parse the output of an LLM call."""
        return text.strip().split(", ")


def test_each_simple() -> None:
    """Test that each() works with a simple runnable."""
    parser = FakeSplitIntoListParser()
    assert parser.invoke("first item, second item") == ["first item", "second item"]
    assert parser.map().invoke(["a, b", "c"]) == [["a", "b"], ["c"]]
    assert parser.map().map().invoke([["a, b", "c"], ["c, e"]]) == [
        [["a", "b"], ["c"]],
        [["c", "e"]],
    ]


def test_each(snapshot: SnapshotAssertion) -> None:
    prompt = (
        SystemMessagePromptTemplate.from_template("You are a nice assistant.")
        + "{question}"
    )
    first_llm = FakeStreamingListLLM(responses=["first item, second item, third item"])
    parser = FakeSplitIntoListParser()
    second_llm = FakeStreamingListLLM(responses=["this", "is", "a", "test"])

    chain = prompt | first_llm | parser | second_llm.map()

    assert dumps(chain, pretty=True) == snapshot
    output = chain.invoke({"question": "What up"})
    assert output == ["this", "is", "a"]

    assert (parser | second_llm.map()).invoke("first item, second item") == [
        "test",
        "this",
    ]


def test_recursive_lambda() -> None:
    def _simple_recursion(x: int) -> Union[int, Runnable]:
        if x < 10:
            return RunnableLambda(lambda *args: _simple_recursion(x + 1))
        else:
            return x

    runnable = RunnableLambda(_simple_recursion)
    assert runnable.invoke(5) == 10

    with pytest.raises(RecursionError):
        runnable.invoke(0, {"recursion_limit": 9})


def test_retrying(mocker: MockerFixture) -> None:
    def _lambda(x: int) -> Union[int, Runnable]:
        if x == 1:
            raise ValueError("x is 1")
        elif x == 2:
            raise RuntimeError("x is 2")
        else:
            return x

    _lambda_mock = mocker.Mock(side_effect=_lambda)
    runnable = RunnableLambda(_lambda_mock)

    with pytest.raises(ValueError):
        runnable.invoke(1)

    assert _lambda_mock.call_count == 1
    _lambda_mock.reset_mock()

    with pytest.raises(ValueError):
        runnable.with_retry(
            stop_after_attempt=2,
            retry_if_exception_type=(ValueError,),
        ).invoke(1)

    assert _lambda_mock.call_count == 2  # retried
    _lambda_mock.reset_mock()

    with pytest.raises(RuntimeError):
        runnable.with_retry(
            stop_after_attempt=2,
            retry_if_exception_type=(ValueError,),
        ).invoke(2)

    assert _lambda_mock.call_count == 1  # did not retry
    _lambda_mock.reset_mock()

    with pytest.raises(ValueError):
        runnable.with_retry(
            stop_after_attempt=2,
            retry_if_exception_type=(ValueError,),
        ).batch([1, 2, 0])

    # 3rd input isn't retried because it succeeded
    assert _lambda_mock.call_count == 3 + 2
    _lambda_mock.reset_mock()

    output = runnable.with_retry(
        stop_after_attempt=2,
        retry_if_exception_type=(ValueError,),
    ).batch([1, 2, 0], return_exceptions=True)

    # 3rd input isn't retried because it succeeded
    assert _lambda_mock.call_count == 3 + 2
    assert len(output) == 3
    assert isinstance(output[0], ValueError)
    assert isinstance(output[1], RuntimeError)
    assert output[2] == 0
    _lambda_mock.reset_mock()


@pytest.mark.asyncio
async def test_async_retrying(mocker: MockerFixture) -> None:
    def _lambda(x: int) -> Union[int, Runnable]:
        if x == 1:
            raise ValueError("x is 1")
        elif x == 2:
            raise RuntimeError("x is 2")
        else:
            return x

    _lambda_mock = mocker.Mock(side_effect=_lambda)
    runnable = RunnableLambda(_lambda_mock)

    with pytest.raises(ValueError):
        await runnable.ainvoke(1)

    assert _lambda_mock.call_count == 1
    _lambda_mock.reset_mock()

    with pytest.raises(ValueError):
        await runnable.with_retry(
            stop_after_attempt=2,
            retry_if_exception_type=(ValueError, KeyError),
        ).ainvoke(1)

    assert _lambda_mock.call_count == 2  # retried
    _lambda_mock.reset_mock()

    with pytest.raises(RuntimeError):
        await runnable.with_retry(
            stop_after_attempt=2,
            retry_if_exception_type=(ValueError,),
        ).ainvoke(2)

    assert _lambda_mock.call_count == 1  # did not retry
    _lambda_mock.reset_mock()

    with pytest.raises(ValueError):
        await runnable.with_retry(
            stop_after_attempt=2,
            retry_if_exception_type=(ValueError,),
        ).abatch([1, 2, 0])

    # 3rd input isn't retried because it succeeded
    assert _lambda_mock.call_count == 3 + 2
    _lambda_mock.reset_mock()

    output = await runnable.with_retry(
        stop_after_attempt=2,
        retry_if_exception_type=(ValueError,),
    ).abatch([1, 2, 0], return_exceptions=True)

    # 3rd input isn't retried because it succeeded
    assert _lambda_mock.call_count == 3 + 2
    assert len(output) == 3
    assert isinstance(output[0], ValueError)
    assert isinstance(output[1], RuntimeError)
    assert output[2] == 0
    _lambda_mock.reset_mock()


@freeze_time("2023-01-01")
def test_seq_batch_return_exceptions(mocker: MockerFixture) -> None:
    class ControlledExceptionRunnable(Runnable[str, str]):
        def __init__(self, fail_starts_with: str) -> None:
            self.fail_starts_with = fail_starts_with

        def invoke(self, input: Any, config: Optional[RunnableConfig] = None) -> Any:
            raise NotImplementedError()

        def _batch(
            self,
            inputs: List[str],
        ) -> List:
            outputs: List[Any] = []
            for input in inputs:
                if input.startswith(self.fail_starts_with):
                    outputs.append(ValueError())
                else:
                    outputs.append(input + "a")
            return outputs

        def batch(
            self,
            inputs: List[str],
            config: Optional[Union[RunnableConfig, List[RunnableConfig]]] = None,
            *,
            return_exceptions: bool = False,
            **kwargs: Any,
        ) -> List[str]:
            return self._batch_with_config(
                self._batch,
                inputs,
                config,
                return_exceptions=return_exceptions,
                **kwargs,
            )

    chain = (
        ControlledExceptionRunnable("bux")
        | ControlledExceptionRunnable("bar")
        | ControlledExceptionRunnable("baz")
        | ControlledExceptionRunnable("foo")
    )

    assert isinstance(chain, RunnableSequence)

    # Test batch
    with pytest.raises(ValueError):
        chain.batch(["foo", "bar", "baz", "qux"])

    spy = mocker.spy(ControlledExceptionRunnable, "batch")
    tracer = FakeTracer()
    inputs = ["foo", "bar", "baz", "qux"]
    outputs = chain.batch(inputs, dict(callbacks=[tracer]), return_exceptions=True)
    assert len(outputs) == 4
    assert isinstance(outputs[0], ValueError)
    assert isinstance(outputs[1], ValueError)
    assert isinstance(outputs[2], ValueError)
    assert outputs[3] == "quxaaaa"
    assert spy.call_count == 4
    inputs_to_batch = [c[0][1] for c in spy.call_args_list]
    assert inputs_to_batch == [
        # inputs to sequence step 0
        # same as inputs to sequence.batch()
        ["foo", "bar", "baz", "qux"],
        # inputs to sequence step 1
        # == outputs of sequence step 0 as no exceptions were raised
        ["fooa", "bara", "baza", "quxa"],
        # inputs to sequence step 2
        # 'bar' was dropped as it raised an exception in step 1
        ["fooaa", "bazaa", "quxaa"],
        # inputs to sequence step 3
        # 'baz' was dropped as it raised an exception in step 2
        ["fooaaa", "quxaaa"],
    ]
    parent_runs = sorted(
        (r for r in tracer.runs if r.parent_run_id is None),
        key=lambda run: inputs.index(run.inputs["input"]),
    )
    assert len(parent_runs) == 4

    parent_run_foo = parent_runs[0]
    assert parent_run_foo.inputs["input"] == "foo"
    assert parent_run_foo.error == repr(ValueError())
    assert len(parent_run_foo.child_runs) == 4
    assert [r.error for r in parent_run_foo.child_runs] == [
        None,
        None,
        None,
        repr(ValueError()),
    ]

    parent_run_bar = parent_runs[1]
    assert parent_run_bar.inputs["input"] == "bar"
    assert parent_run_bar.error == repr(ValueError())
    assert len(parent_run_bar.child_runs) == 2
    assert [r.error for r in parent_run_bar.child_runs] == [
        None,
        repr(ValueError()),
    ]

    parent_run_baz = parent_runs[2]
    assert parent_run_baz.inputs["input"] == "baz"
    assert parent_run_baz.error == repr(ValueError())
    assert len(parent_run_baz.child_runs) == 3
    assert [r.error for r in parent_run_baz.child_runs] == [
        None,
        None,
        repr(ValueError()),
    ]

    parent_run_qux = parent_runs[3]
    assert parent_run_qux.inputs["input"] == "qux"
    assert parent_run_qux.error is None
    assert parent_run_qux.outputs["output"] == "quxaaaa"
    assert len(parent_run_qux.child_runs) == 4
    assert [r.error for r in parent_run_qux.child_runs] == [None, None, None, None]


@pytest.mark.asyncio
@freeze_time("2023-01-01")
async def test_seq_abatch_return_exceptions(mocker: MockerFixture) -> None:
    class ControlledExceptionRunnable(Runnable[str, str]):
        def __init__(self, fail_starts_with: str) -> None:
            self.fail_starts_with = fail_starts_with

        def invoke(self, input: Any, config: Optional[RunnableConfig] = None) -> Any:
            raise NotImplementedError()

        async def _abatch(
            self,
            inputs: List[str],
        ) -> List:
            outputs: List[Any] = []
            for input in inputs:
                if input.startswith(self.fail_starts_with):
                    outputs.append(ValueError())
                else:
                    outputs.append(input + "a")
            return outputs

        async def abatch(
            self,
            inputs: List[str],
            config: Optional[Union[RunnableConfig, List[RunnableConfig]]] = None,
            *,
            return_exceptions: bool = False,
            **kwargs: Any,
        ) -> List[str]:
            return await self._abatch_with_config(
                self._abatch,
                inputs,
                config,
                return_exceptions=return_exceptions,
                **kwargs,
            )

    chain = (
        ControlledExceptionRunnable("bux")
        | ControlledExceptionRunnable("bar")
        | ControlledExceptionRunnable("baz")
        | ControlledExceptionRunnable("foo")
    )

    assert isinstance(chain, RunnableSequence)

    # Test abatch
    with pytest.raises(ValueError):
        await chain.abatch(["foo", "bar", "baz", "qux"])

    spy = mocker.spy(ControlledExceptionRunnable, "abatch")
    tracer = FakeTracer()
    inputs = ["foo", "bar", "baz", "qux"]
    outputs = await chain.abatch(
        inputs, dict(callbacks=[tracer]), return_exceptions=True
    )
    assert len(outputs) == 4
    assert isinstance(outputs[0], ValueError)
    assert isinstance(outputs[1], ValueError)
    assert isinstance(outputs[2], ValueError)
    assert outputs[3] == "quxaaaa"
    assert spy.call_count == 4
    inputs_to_batch = [c[0][1] for c in spy.call_args_list]
    assert inputs_to_batch == [
        # inputs to sequence step 0
        # same as inputs to sequence.batch()
        ["foo", "bar", "baz", "qux"],
        # inputs to sequence step 1
        # == outputs of sequence step 0 as no exceptions were raised
        ["fooa", "bara", "baza", "quxa"],
        # inputs to sequence step 2
        # 'bar' was dropped as it raised an exception in step 1
        ["fooaa", "bazaa", "quxaa"],
        # inputs to sequence step 3
        # 'baz' was dropped as it raised an exception in step 2
        ["fooaaa", "quxaaa"],
    ]
    parent_runs = sorted(
        (r for r in tracer.runs if r.parent_run_id is None),
        key=lambda run: inputs.index(run.inputs["input"]),
    )
    assert len(parent_runs) == 4

    parent_run_foo = parent_runs[0]
    assert parent_run_foo.inputs["input"] == "foo"
    assert parent_run_foo.error == repr(ValueError())
    assert len(parent_run_foo.child_runs) == 4
    assert [r.error for r in parent_run_foo.child_runs] == [
        None,
        None,
        None,
        repr(ValueError()),
    ]

    parent_run_bar = parent_runs[1]
    assert parent_run_bar.inputs["input"] == "bar"
    assert parent_run_bar.error == repr(ValueError())
    assert len(parent_run_bar.child_runs) == 2
    assert [r.error for r in parent_run_bar.child_runs] == [
        None,
        repr(ValueError()),
    ]

    parent_run_baz = parent_runs[2]
    assert parent_run_baz.inputs["input"] == "baz"
    assert parent_run_baz.error == repr(ValueError())
    assert len(parent_run_baz.child_runs) == 3
    assert [r.error for r in parent_run_baz.child_runs] == [
        None,
        None,
        repr(ValueError()),
    ]

    parent_run_qux = parent_runs[3]
    assert parent_run_qux.inputs["input"] == "qux"
    assert parent_run_qux.error is None
    assert parent_run_qux.outputs["output"] == "quxaaaa"
    assert len(parent_run_qux.child_runs) == 4
    assert [r.error for r in parent_run_qux.child_runs] == [None, None, None, None]


def test_runnable_branch_init() -> None:
    """Verify that runnable branch gets initialized properly."""
    add = RunnableLambda(lambda x: x + 1)
    condition = RunnableLambda(lambda x: x > 0)

    # Test failure with less than 2 branches
    with pytest.raises(ValueError):
        RunnableBranch((condition, add))

    # Test failure with less than 2 branches
    with pytest.raises(ValueError):
        RunnableBranch(condition)


@pytest.mark.parametrize(
    "branches",
    [
        [
            (RunnableLambda(lambda x: x > 0), RunnableLambda(lambda x: x + 1)),
            RunnableLambda(lambda x: x - 1),
        ],
        [
            (RunnableLambda(lambda x: x > 0), RunnableLambda(lambda x: x + 1)),
            (RunnableLambda(lambda x: x > 5), RunnableLambda(lambda x: x + 1)),
            RunnableLambda(lambda x: x - 1),
        ],
        [
            (lambda x: x > 0, lambda x: x + 1),
            (lambda x: x > 5, lambda x: x + 1),
            lambda x: x - 1,
        ],
    ],
)
def test_runnable_branch_init_coercion(branches: Sequence[Any]) -> None:
    """Verify that runnable branch gets initialized properly."""
    runnable = RunnableBranch[int, int](*branches)
    for branch in runnable.branches:
        condition, body = branch
        assert isinstance(condition, Runnable)
        assert isinstance(body, Runnable)

    assert isinstance(runnable.default, Runnable)
    assert runnable.input_schema.schema() == {"title": "RunnableBranchInput"}


def test_runnable_branch_invoke_call_counts(mocker: MockerFixture) -> None:
    """Verify that runnables are invoked only when necessary."""
    # Test with single branch
    add = RunnableLambda(lambda x: x + 1)
    sub = RunnableLambda(lambda x: x - 1)
    condition = RunnableLambda(lambda x: x > 0)
    spy = mocker.spy(condition, "invoke")
    add_spy = mocker.spy(add, "invoke")

    branch = RunnableBranch[int, int]((condition, add), (condition, add), sub)
    assert spy.call_count == 0
    assert add_spy.call_count == 0

    assert branch.invoke(1) == 2
    assert add_spy.call_count == 1
    assert spy.call_count == 1

    assert branch.invoke(2) == 3
    assert spy.call_count == 2
    assert add_spy.call_count == 2

    assert branch.invoke(-3) == -4
    # Should fall through to default branch with condition being evaluated twice!
    assert spy.call_count == 4
    # Add should not be invoked
    assert add_spy.call_count == 2


def test_runnable_branch_invoke() -> None:
    # Test with single branch
    def raise_value_error(x: int) -> int:
        """Raise a value error."""
        raise ValueError("x is too large")

    branch = RunnableBranch[int, int](
        (lambda x: x > 100, raise_value_error),
        # mypy cannot infer types from the lambda
        (lambda x: x > 0 and x < 5, lambda x: x + 1),  # type: ignore[misc]
        (lambda x: x > 5, lambda x: x * 10),
        lambda x: x - 1,
    )

    assert branch.invoke(1) == 2
    assert branch.invoke(10) == 100
    assert branch.invoke(0) == -1
    # Should raise an exception
    with pytest.raises(ValueError):
        branch.invoke(1000)


def test_runnable_branch_batch() -> None:
    """Test batch variant."""
    # Test with single branch
    branch = RunnableBranch[int, int](
        (lambda x: x > 0 and x < 5, lambda x: x + 1),
        (lambda x: x > 5, lambda x: x * 10),
        lambda x: x - 1,
    )

    assert branch.batch([1, 10, 0]) == [2, 100, -1]


@pytest.mark.asyncio
async def test_runnable_branch_ainvoke() -> None:
    """Test async variant of invoke."""
    branch = RunnableBranch[int, int](
        (lambda x: x > 0 and x < 5, lambda x: x + 1),
        (lambda x: x > 5, lambda x: x * 10),
        lambda x: x - 1,
    )

    assert await branch.ainvoke(1) == 2
    assert await branch.ainvoke(10) == 100
    assert await branch.ainvoke(0) == -1

    # Verify that the async variant is used if available
    async def condition(x: int) -> bool:
        return x > 0

    async def add(x: int) -> int:
        return x + 1

    async def sub(x: int) -> int:
        return x - 1

    branch = RunnableBranch[int, int]((condition, add), sub)

    assert await branch.ainvoke(1) == 2
    assert await branch.ainvoke(-10) == -11


def test_runnable_branch_invoke_callbacks() -> None:
    """Verify that callbacks are correctly used in invoke."""
    tracer = FakeTracer()

    def raise_value_error(x: int) -> int:
        """Raise a value error."""
        raise ValueError("x is too large")

    branch = RunnableBranch[int, int](
        (lambda x: x > 100, raise_value_error),
        lambda x: x - 1,
    )

    assert branch.invoke(1, config={"callbacks": [tracer]}) == 0
    assert len(tracer.runs) == 1
    assert tracer.runs[0].error is None
    assert tracer.runs[0].outputs == {"output": 0}

    # Check that the chain on end is invoked
    with pytest.raises(ValueError):
        branch.invoke(1000, config={"callbacks": [tracer]})

    assert len(tracer.runs) == 2
    assert tracer.runs[1].error == "ValueError('x is too large')"
    assert tracer.runs[1].outputs is None


@pytest.mark.asyncio
async def test_runnable_branch_ainvoke_callbacks() -> None:
    """Verify that callbacks are invoked correctly in ainvoke."""
    tracer = FakeTracer()

    async def raise_value_error(x: int) -> int:
        """Raise a value error."""
        raise ValueError("x is too large")

    branch = RunnableBranch[int, int](
        (lambda x: x > 100, raise_value_error),
        lambda x: x - 1,
    )

    assert await branch.ainvoke(1, config={"callbacks": [tracer]}) == 0
    assert len(tracer.runs) == 1
    assert tracer.runs[0].error is None
    assert tracer.runs[0].outputs == {"output": 0}

    # Check that the chain on end is invoked
    with pytest.raises(ValueError):
        await branch.ainvoke(1000, config={"callbacks": [tracer]})

    assert len(tracer.runs) == 2
    assert tracer.runs[1].error == "ValueError('x is too large')"
    assert tracer.runs[1].outputs is None


@pytest.mark.asyncio
async def test_runnable_branch_abatch() -> None:
    """Test async variant of invoke."""
    branch = RunnableBranch[int, int](
        (lambda x: x > 0 and x < 5, lambda x: x + 1),
        (lambda x: x > 5, lambda x: x * 10),
        lambda x: x - 1,
    )

    assert await branch.abatch([1, 10, 0]) == [2, 100, -1]
