"""The reader that asks a chat-completions endpoint, against a stand-in server."""

import json

import httpx
import pytest

from osint_agent.cases import ChatReader, ReaderError
from osint_agent.cases.chat_reader import extract_flagged

FLAGGED = [{"id": "P2", "kind": "statement", "quote": "burn their office down"}]


def tool_reply(flagged: list, arguments_as_text: bool = True) -> httpx.Response:
    arguments = {"flagged": flagged}
    call = {
        "function": {
            "name": "submit_review",
            "arguments": json.dumps(arguments) if arguments_as_text else arguments,
        }
    }
    return httpx.Response(
        200, json={"choices": [{"message": {"content": "", "tool_calls": [call]}}]}
    )


def text_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def make_reader(
    handler, **settings
) -> tuple[ChatReader, list[httpx.Request], list[float]]:
    requests: list[httpx.Request] = []
    waits: list[float] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    reader = ChatReader(
        "http://model.example/v1/",
        "test-model",
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(recording)),
        wait=waits.append,
        **settings,
    )
    return reader, requests, waits


def test_should_return_the_reported_posts_when_the_model_calls_the_tool():
    reader, _, _ = make_reader(lambda request: tool_reply(FLAGGED))

    assert reader.read("instructions", "[P2] 2025-01-01 post: words") == FLAGGED


def test_should_accept_tool_arguments_that_arrive_already_parsed():
    reader, _, _ = make_reader(
        lambda request: tool_reply(FLAGGED, arguments_as_text=False)
    )

    assert reader.read("instructions", "listing") == FLAGGED


def test_should_send_the_instructions_the_batch_and_the_settings_in_one_fresh_conversation():
    reader, requests, _ = make_reader(lambda request: tool_reply([]))

    reader.read("the instructions", "the listing")

    sent = json.loads(requests[0].content)
    assert str(requests[0].url) == "http://model.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert [message["role"] for message in sent["messages"]] == ["system", "user"]
    assert sent["messages"][0]["content"] == "the instructions"
    assert sent["messages"][1]["content"].endswith("the listing")
    assert sent["tools"][0]["function"]["name"] == "submit_review"
    assert (sent["model"], sent["reasoning_effort"], sent["temperature"]) == (
        "test-model",
        "none",
        0.2,
    )


def test_should_leave_out_the_effort_setting_when_none_is_wanted():
    reader, requests, _ = make_reader(lambda request: tool_reply([]), effort=None)

    reader.read("instructions", "listing")

    assert "reasoning_effort" not in json.loads(requests[0].content)


def test_should_read_the_answer_from_text_when_the_model_makes_no_tool_call():
    answer = '{"flagged": [{"id": "P2", "kind": "statement", "quote": "q"}]}'
    reply = f"Here it is:\n```json\n{answer}\n```"
    reader, _, _ = make_reader(lambda request: text_reply(reply))

    assert reader.read("instructions", "listing")[0]["id"] == "P2"


def test_should_ask_once_more_then_give_up_when_the_answer_cannot_be_understood():
    reader, requests, _ = make_reader(
        lambda request: text_reply("I reviewed them all.")
    )

    with pytest.raises(ReaderError, match="could not be understood"):
        reader.read("instructions", "listing")

    assert len(requests) == 2


def test_should_wait_as_asked_and_try_again_when_the_endpoint_is_busy():
    replies = iter(
        [httpx.Response(503, headers={"Retry-After": "7"}), tool_reply(FLAGGED)]
    )
    reader, requests, waits = make_reader(lambda request: next(replies))

    assert reader.read("instructions", "listing") == FLAGGED
    assert (len(requests), waits) == (2, [7.0])


def test_should_fail_plainly_when_the_endpoint_answers_with_an_error():
    reader, _, _ = make_reader(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(ReaderError, match="HTTP 500"):
        reader.read("instructions", "listing")


def test_should_fail_plainly_without_the_key_when_the_endpoint_cannot_be_reached():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    reader, _, _ = make_reader(refuse)

    with pytest.raises(ReaderError, match="could not be reached") as failure:
        reader.read("instructions", "listing")

    assert "test-key" not in str(failure.value)


def test_should_rest_after_a_batch_when_a_pause_is_set():
    reader, _, waits = make_reader(lambda request: tool_reply([]), pause=3.0)

    reader.read("instructions", "listing")

    assert waits == [3.0]


def test_should_build_a_reader_from_the_environment_only_when_it_is_configured():
    configured = ChatReader.from_env(
        {
            "LLM_BASE_URL": "http://model.example/v1",
            "LLM_MODEL": "test-model",
            "LLM_PAUSE_SECONDS": "5",
        }
    )

    assert ChatReader.from_env({}) is None
    assert ChatReader.from_env({"LLM_BASE_URL": "http://model.example/v1"}) is None
    assert (configured.name, configured.effort, configured.pause) == (
        "test-model",
        "none",
        5.0,
    )


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"choices": []},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"tool_calls": [{"function": {"name": "other"}}]}}]},
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "submit_review", "arguments": "{"}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"message": {"content": '{"flagged": "all of them"}'}}]},
    ],
)
def test_should_find_no_answer_in_a_reply_of_the_wrong_shape(data):
    flagged, why = extract_flagged(data)

    assert flagged is None
    assert why
