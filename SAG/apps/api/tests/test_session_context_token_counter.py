from types import SimpleNamespace
from sag_api.services.session_context.model import ModelTokenCounter


def test_transformers_batch_encoding_counts_input_ids_not_dictionary_keys():
    counter = object.__new__(ModelTokenCounter)
    counter.template_kwargs = {"enable_thinking": False}
    seen = []

    def tokenize(messages, **kwargs):
        seen.append((messages, kwargs))
        return {"input_ids": list(range(1234)), "attention_mask": [1] * 1234}

    counter.tokenizer = SimpleNamespace(apply_chat_template=tokenize)
    messages = [{"role": "user", "content": "current"}]
    tools = [{"type": "function", "function": {"name": "search"}}]
    assert counter.count(messages, tools) == 1490
    assert seen[0][0] == messages and seen[0][1]["tools"] == tools and seen[0][1]["enable_thinking"] is False
