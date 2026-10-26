"""Graph math with explicit synthetic features, separate from learned model smoke."""

import hashlib
from dataclasses import replace

import pytest
from test_session_context_reader import authority, rows, work

from sag_api.services.session_context.graph import (
    EntityMention,
    LocalFeatureEncoder,
    TextFeatures,
    build_graph,
    graph_scores,
)
from sag_api.services.session_context.reader import FixtureTraceStore, read_history
from sag_api.services.session_context.types import ContextContractError


def feature(text, vector=(1.0, 0.0), entities=(), model="fixture-v1"):
    mentions = tuple(EntityMention(text.index(word), text.index(word) + len(word), "ORG") for word in entities)
    return TextFeatures(hashlib.sha256(text.encode()).hexdigest(), model, vector, mentions)


def fixture(texts, *, entities=None, vectors=None, denied=()):
    messages = tuple(replace(m, content=text, provenance="unknown" if i in denied else "recorded")
                     for i, (m, text) in enumerate(zip(rows(len(texts)), texts, strict=True)))
    budget, scope = work(1000000), authority()
    store = FixtureTraceStore(messages, scope=scope, generation="g1", budget=budget)
    history = read_history(store, scope=scope, page_size=1, budget=budget, now=100)
    by_id = {m.message_id: i for i, m in enumerate(messages)}
    features = tuple(feature(m.content, vector=vectors[by_id[m.message_id]] if vectors else (1.0, 0.0),
                             entities=entities[by_id[m.message_id]] if entities else ()) for m in history.messages)
    graph = build_graph(history, features, scope=scope, budget=budget, gap_seconds=600, now=100)
    return messages, graph, budget


def test_entity_context_graph_keeps_observed_bridge_and_weighted_frequencies():
    _, graph, budget = fixture(["Alpha owns Beta.", "Beta owns Gamma.", "Gamma uses PIN-472."],
                               entities=[("Alpha", "Beta"), ("Beta", "Gamma"), ("Gamma",)])
    assert set(graph.entity_keys) == {"ORG:alpha", "ORG:beta", "ORG:gamma"}
    assert len(graph.features) == 3
    scores = graph_scores(graph, "Alpha?", feature("Alpha?", entities=("Alpha",)), scope=authority(),
                          budget=budget, steps=20, damping=0.6, now=100)
    assert all(value > 0 for value in scores.values())
    assert scores["m00000"] > scores["m00002"]


def test_no_entity_anchor_uses_dense_fallback_instead_of_uniform_pagerank():
    _, graph, budget = fixture(["old fact", "correct meaning", "distractor"],
                               vectors=[(0.0, 1.0), (1.0, 0.0), (-1.0, 0.0)])
    scores = graph_scores(graph, "meaning?", feature("meaning?"), scope=authority(), budget=budget,
                          steps=20, damping=0.6, now=100)
    assert scores["m00001"] == 1
    assert scores["m00000"] == scores["m00002"] == 0


def test_denied_row_never_enters_entities_or_adjacency():
    _, graph, _ = fixture(["Alpha", "SECRET", "Beta"], entities=[("Alpha",), ("SECRET",), ("Beta",)], denied=(1,))
    assert "ORG:secret" not in graph.entity_keys
    assert not any(left < 2 and right < 2 for left, right, _ in graph.edges)


@pytest.mark.parametrize("change", [{"model_id": "different"}, {"content_hash": "0" * 64},
                                    {"vector": (float("nan"), 0.0)}, {"vector": (0.0, 0.0)}])
def test_invalid_or_stale_features_fail_before_ranking(change):
    _, graph, budget = fixture(["Alpha"])
    with pytest.raises(ContextContractError):
        query = replace(feature("Alpha"), **change)
        graph_scores(graph, "Alpha", query, scope=authority(), budget=budget, steps=2, damping=0.6, now=100)


def test_wrong_scope_expiry_or_query_dimension_are_rejected():
    _, graph, budget = fixture(["Alpha"])
    for scope, instant, query in ((replace(authority(), source_ids=frozenset()), 100, feature("Alpha")),
                                  (authority(), 2000, feature("Alpha")),
                                  (authority(), 100, feature("Alpha", vector=(1.0,)))):
        with pytest.raises(ContextContractError):
            graph_scores(graph, "Alpha", query, scope=scope, budget=budget, steps=2, damping=0.6, now=instant)


def test_graph_build_rejects_features_from_a_different_message():
    _, graph, budget = fixture(["Alpha"])
    with pytest.raises(ContextContractError):
        build_graph(graph.history, (feature("Beta"),), scope=authority(), budget=budget, gap_seconds=600, now=100)


def test_duplicate_mentions_cannot_inflate_observed_frequency():
    mention = EntityMention(0, 5, "ORG")
    with pytest.raises(ContextContractError, match="duplicate"):
        TextFeatures(feature("Alpha").content_hash, "fixture-v1", (1.0, 0.0), (mention, mention))


def test_real_mention_occurrences_produce_frequency_normalized_edges():
    _, graph, budget = fixture(["Alpha Alpha Beta"])
    observed = replace(graph.features[0], mentions=(EntityMention(0, 5, "ORG"), EntityMention(6, 11, "ORG"),
                                                   EntityMention(12, 16, "ORG")))
    graph = build_graph(graph.history, (observed,), scope=authority(), budget=budget, gap_seconds=600, now=100)
    assert graph.edges == ((0, 1, 2 / 3), (1, 0, 2 / 3), (0, 2, 1 / 3), (2, 0, 1 / 3))


@pytest.mark.parametrize("steps,damping", [(0, 0.6), (True, 0.6), (2, float("nan")), (2, 1.0)])
def test_pagerank_cannot_run_with_invalid_or_unbounded_parameters(steps, damping):
    _, graph, budget = fixture(["Alpha"])
    with pytest.raises(ContextContractError):
        graph_scores(graph, "Alpha", feature("Alpha"), scope=authority(), budget=budget,
                     steps=steps, damping=damping, now=100)


def stub_encoder(chunk_size):
    class Encoder:
        max_seq_length = 10

        @staticmethod
        def tokenizer(text, *, truncation):
            assert not truncation
            return {"input_ids": list(range(len(text) + 2))}

        @staticmethod
        def get_embedding_dimension():
            return 2

        @staticmethod
        def encode(*args, **kwargs):
            return (1.0, 0.0)

    class Ner:
        tokenizer = staticmethod(Encoder.tokenizer)

        def __call__(self, text):
            return [{"start": 0, "end": 1, "entity_group": "LOC"}] if text else []

    encoder = LocalFeatureEncoder.__new__(LocalFeatureEncoder)
    encoder.encoder, encoder.ner = Encoder(), Ner()
    encoder.chunk_codepoints, encoder.model_id = chunk_size, "explicit-stub-model"
    encoder.encoder_calls = encoder.encoder_tokens = encoder.ner_calls = encoder.ner_tokens = 0
    return encoder


def test_chunk_adapter_keeps_original_unicode_offsets_and_separate_usage():
    encoder, budget = stub_encoder(4), work()
    features = encoder.featurize("🙂abcDEF", budget=budget)
    assert features.mentions == (EntityMention(0, 1, "LOC"), EntityMention(4, 5, "LOC"))
    assert encoder.encoder_calls == encoder.ner_calls == 2
    assert encoder.encoder_tokens == encoder.ner_tokens == 11
    assert budget.calls == 0


def test_overlong_chunk_is_rejected_before_inference_instead_of_truncated():
    encoder = stub_encoder(20)
    with pytest.raises(ContextContractError, match="chunk_too_long"):
        encoder.featurize("a" * 15, budget=work())
    assert encoder.encoder_calls == encoder.ner_calls == 0
