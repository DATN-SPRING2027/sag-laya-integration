"""Offline observed-entity graph and local learned features; no generative calls."""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sag_api.services.session_context.budget import BudgetState
from sag_api.services.session_context.reader import HistoryRead, order_key, trace_work
from sag_api.services.session_context.scope import _check_time, eligible_message_ids
from sag_api.services.session_context.types import SessionScope, require

ENCODER = ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181")
NER = ("OpenMed/OpenMed-PII-Vietnamese-BigMed-Large-278M-v1", "f253fd932f0508be1eac3d1323e41bc3efe2ef70")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EntityMention:
    start: int
    end: int
    label: str

    def __post_init__(self):
        require(type(self.start) is int and type(self.end) is int and 0 <= self.start < self.end, "entity_offsets")
        require(isinstance(self.label, str) and bool(self.label.strip()), "entity_label")


def observed_mentions(entities, *, offset=0) -> tuple[EntityMention, ...]:
    """Declared label mapping; detected secrets do not become graph entities."""
    labels = {"PER": "PER", "ORG": "ORG", "LOC": "LOC", "FIRSTNAME": "PER", "MIDDLENAME": "PER",
              "LASTNAME": "PER", "ORGANIZATION": "ORG", "CITY": "LOC", "COUNTY": "LOC", "STATE": "LOC",
              "STREET": "LOC"}
    return tuple(EntityMention(offset + int(e["start"]), offset + int(e["end"]), labels[e["entity_group"]])
                 for e in entities if e["entity_group"] in labels)


@dataclass(frozen=True, slots=True)
class TextFeatures:
    content_hash: str
    model_id: str
    vector: tuple[float, ...] = field(repr=False)
    mentions: tuple[EntityMention, ...] = ()

    def __post_init__(self):
        require(isinstance(self.content_hash, str) and len(self.content_hash) == 64, "feature_hash")
        require(isinstance(self.model_id, str) and bool(self.model_id.strip()), "feature_model")
        require(isinstance(self.vector, tuple) and bool(self.vector)
                and all(type(v) in (int, float) and math.isfinite(v) for v in self.vector), "feature_vector")
        require(0 < math.hypot(*self.vector) < math.inf, "feature_vector_norm")
        require(isinstance(self.mentions, tuple) and all(isinstance(e, EntityMention) for e in self.mentions),
                "feature_mentions")
        require(len(set(self.mentions)) == len(self.mentions), "duplicate_entity_mention")


def entity_keys(text: str, features: TextFeatures) -> tuple[str, ...]:
    require(features.content_hash == text_hash(text), "feature_content_changed")
    result = []
    for mention in features.mentions:
        require(mention.end <= len(text), "entity_offsets")
        surface = unicodedata.normalize("NFC", text[mention.start:mention.end]).casefold().strip()
        require(bool(surface), "empty_entity")
        result.append(f"{mention.label}:{surface}")
    return tuple(result)


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    require(len(left) == len(right), "feature_dimensions")
    a, b = math.hypot(*left), math.hypot(*right)
    return max(0.0, min(1.0, math.fsum((x / a) * (y / b) for x, y in zip(left, right, strict=True))))


class LocalFeatureEncoder:
    """Explicit optional load from pinned local cache, with no silent truncation.

    Raw chunks can split entities; this is not semantic segmentation. Metrics
    count encoder and NER inference separately from BudgetState.calls (LLMs).
    """

    def __init__(self, *, chunk_codepoints: int, budget: BudgetState):
        require(type(chunk_codepoints) is int and chunk_codepoints > 0, "feature_chunk")
        budget.consume_index_units(1)
        from sentence_transformers import SentenceTransformer
        from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline

        self.encoder = SentenceTransformer(ENCODER[0], revision=ENCODER[1], local_files_only=True,
                                          trust_remote_code=False, token=False, device="cpu",
                                          model_kwargs={"weights_only": True})
        tokenizer = AutoTokenizer.from_pretrained(NER[0], revision=NER[1], local_files_only=True,
                                                  trust_remote_code=False, token=False)
        model = AutoModelForTokenClassification.from_pretrained(
            NER[0], revision=NER[1], local_files_only=True, trust_remote_code=False, token=False,
            use_safetensors=True)
        self.ner = pipeline("token-classification", model=model, tokenizer=tokenizer,
                            aggregation_strategy="simple", device="cpu")
        self.chunk_codepoints = chunk_codepoints
        self.model_id = (f"{ENCODER[0]}@{ENCODER[1]}|{NER[0]}@{NER[1]}|raw-chunk-mean:{chunk_codepoints}"
                         "|entities-PER-ORG-LOC-v1")
        self.encoder_calls = self.encoder_tokens = self.ner_calls = self.ner_tokens = 0
        budget.consume_index_units(0)

    def featurize(self, text: str, *, budget: BudgetState) -> TextFeatures:
        budget.consume_index_units(1 + (len(text) + 1023) // 1024)
        vectors, mentions = [], set()
        for start in range(0, max(1, len(text)), self.chunk_codepoints):
            budget.consume_index_units(1)
            chunk = text[start:start + self.chunk_codepoints]
            encoder_tokens = len(self.encoder.tokenizer(chunk, truncation=False)["input_ids"])
            ner_tokens = len(self.ner.tokenizer(chunk, truncation=False)["input_ids"])
            require(encoder_tokens <= self.encoder.max_seq_length and ner_tokens <= 512, "feature_chunk_too_long")
            budget.consume_index_units(encoder_tokens + ner_tokens + 2
                                       + self.encoder.get_embedding_dimension())
            self.encoder_calls += 1
            self.encoder_tokens += encoder_tokens
            vectors.append(tuple(float(v) for v in self.encoder.encode(chunk, normalize_embeddings=True,
                                                                        show_progress_bar=False)))
            budget.consume_index_units(0)
            self.ner_calls += 1
            self.ner_tokens += ner_tokens
            mentions.update(observed_mentions(self.ner(chunk), offset=start))
            budget.consume_index_units(0)
        budget.consume_index_units(sum(len(vector) for vector in vectors))
        vector = tuple(math.fsum(values) / len(vectors) for values in zip(*vectors, strict=True))
        return TextFeatures(text_hash(text), self.model_id, vector,
                            tuple(sorted(mentions, key=lambda e: (e.start, e.end, e.label))))

    def featurize_many(self, texts: tuple[str, ...], *, batch_size: int,
                       budget: BudgetState, deduplicate: bool = False) -> tuple[TextFeatures, ...]:
        """Bounded inference batches over raw chunks, with optional per-call reuse.

        Calls count physical batch invocations; token counts sum unpadded inputs.
        The serial featurize path remains available for equivalence checking.
        Reuse is exact text only and is discarded on return, never across scopes.
        """
        require(isinstance(texts, tuple) and all(isinstance(text, str) for text in texts), "feature_texts")
        require(type(batch_size) is int and 1 <= batch_size <= 32, "feature_batch")
        require(type(deduplicate) is bool, "feature_deduplicate")
        chunks = []
        for index, text in enumerate(texts):
            budget.consume_index_units(1 + len(text))
            chunks.extend((index, start, text[start:start + self.chunk_codepoints])
                          for start in range(0, max(1, len(text)), self.chunk_codepoints))
        vectors = [[] for _ in texts]
        mentions = [set() for _ in texts]
        dimension = self.encoder.get_embedding_dimension()
        requests = [chunk for _, _, chunk in chunks]
        if deduplicate:
            requests = list(dict.fromkeys(requests))
        encoded_results, detected_results = [], []
        for offset in range(0, len(requests), batch_size):
            budget.consume_index_units(batch_size)
            inputs = requests[offset:offset + batch_size]
            encoder_counts = [len(self.encoder.tokenizer(text, truncation=False)["input_ids"]) for text in inputs]
            ner_counts = [len(self.ner.tokenizer(text, truncation=False)["input_ids"]) for text in inputs]
            require(all(count <= self.encoder.max_seq_length for count in encoder_counts)
                    and all(count <= 512 for count in ner_counts), "feature_chunk_too_long")
            budget.consume_index_units(sum(encoder_counts) + sum(ner_counts) + dimension * len(inputs) + 2)
            self.encoder_calls += 1
            self.encoder_tokens += sum(encoder_counts)
            encoded = self.encoder.encode(inputs, batch_size=len(inputs), normalize_embeddings=True,
                                          show_progress_bar=False)
            budget.consume_index_units(0)
            self.ner_calls += 1
            self.ner_tokens += sum(ner_counts)
            detected = self.ner(inputs, batch_size=len(inputs))
            budget.consume_index_units(0)
            for vector, entities in zip(encoded, detected, strict=True):
                encoded_results.append(tuple(float(v) for v in vector))
                detected_results.append(observed_mentions(entities))
        request_indices = {text: i for i, text in enumerate(requests)} if deduplicate else {}
        for position, (index, start, text) in enumerate(chunks):
            result_index = request_indices[text] if deduplicate else position
            budget.consume_index_units(dimension + len(detected_results[result_index]))
            vectors[index].append(encoded_results[result_index])
            for entity in detected_results[result_index]:
                mentions[index].add(EntityMention(start + entity.start, start + entity.end, entity.label))
        result = []
        for text, parts, entities in zip(texts, vectors, mentions, strict=True):
            budget.consume_index_units(len(parts) * dimension)
            vector = tuple(math.fsum(values) / len(parts) for values in zip(*parts, strict=True))
            result.append(TextFeatures(text_hash(text), self.model_id, vector,
                                       tuple(sorted(entities, key=lambda e: (e.start, e.end, e.label)))))
        return tuple(result)


@dataclass(frozen=True, slots=True)
class TraceGraph:
    history: HistoryRead = field(repr=False)
    features: tuple[TextFeatures, ...] = field(repr=False)
    entity_keys: tuple[str, ...]
    context_entities: tuple[tuple[str, ...], ...]
    edges: tuple[tuple[int, int, float], ...]


def build_graph(history: HistoryRead, features: tuple[TextFeatures, ...], *, scope: SessionScope,
                budget: BudgetState, gap_seconds: float, now: float | None = None) -> TraceGraph:
    _check_time(scope, now)
    require(history.snapshot.scope_fingerprint == scope.fingerprint, "graph_scope")
    require(type(gap_seconds) in (int, float) and math.isfinite(gap_seconds) and gap_seconds > 0, "time_gap")
    require(isinstance(features, tuple) and len(features) == len(history.messages) == len(history.ordinals),
            "graph_features")
    records, contexts = {}, []
    for message, feature in zip(history.messages, features, strict=True):
        require(isinstance(feature, TextFeatures), "graph_features")
        budget.consume_index_units(trace_work(message) + len(feature.vector) + len(feature.mentions))
        require(message.message_id not in records, "duplicate_message")
        records[message.message_id] = message
        contexts.append(entity_keys(message.content, feature))
        require(feature.model_id == features[0].model_id and len(feature.vector) == len(features[0].vector),
                "feature_model_changed")
    require(eligible_message_ids(scope, records, now=now) == history.coverage.eligible_ids, "graph_coverage")
    keys = tuple(sorted({key for context in contexts for key in context}))
    budget.consume_index_units(len(keys) * max(1, len(keys).bit_length()))
    entity_nodes = {key: len(features) + i for i, key in enumerate(keys)}
    edges = []

    def connect(left, right, weight):
        budget.consume_index_units(2)
        edges.extend(((left, right, weight), (right, left, weight)))

    for index, context in enumerate(contexts):
        for key, count in Counter(context).items():
            connect(index, entity_nodes[key], count / len(context))
        if index and history.ordinals[index] == history.ordinals[index - 1] + 1:
            gap = (order_key(history.messages[index])[0] - order_key(history.messages[index - 1])[0]).total_seconds()
            if 0 <= gap <= gap_seconds:
                connect(index - 1, index, 1.0)
    budget.consume_index_units(0)
    return TraceGraph(history, features, keys, tuple(contexts), tuple(edges))


def graph_scores(graph: TraceGraph, text: str, query: TextFeatures, *, scope: SessionScope,
                 budget: BudgetState, steps: int, damping: float, now: float | None = None) -> dict[str, float]:
    _check_time(scope, now)
    require(graph.history.snapshot.scope_fingerprint == scope.fingerprint, "graph_scope")
    require(type(steps) is int and steps > 0 and type(damping) in (int, float) and 0 < damping < 1, "pagerank_limits")
    budget.consume_index_units(1 + len(text) + len(query.vector) + len(query.mentions))
    anchors = set(entity_keys(text, query)) & set(graph.entity_keys)
    dense = []
    for feature in graph.features:
        require(feature.model_id == query.model_id, "feature_model_changed")
        budget.consume_index_units(len(feature.vector))
        dense.append(cosine(feature.vector, query.vector))
    ids = [message.message_id for message in graph.history.messages]
    if not anchors or not dense:
        return dict(zip(ids, dense, strict=True))
    count = len(dense) + len(graph.entity_keys)
    budget.consume_index_units(count + len(graph.edges))
    reset = dense + [1.0 if key in anchors else 0.0 for key in graph.entity_keys]
    total = math.fsum(reset)
    reset = [value / total for value in reset]
    adjacency = defaultdict(list)
    totals = defaultdict(float)
    for left, right, weight in graph.edges:
        adjacency[left].append((right, weight))
        totals[left] += weight
    scores = reset[:]
    for _ in range(steps):
        budget.consume_index_units(count + len(graph.edges))
        dangling = math.fsum(scores[i] for i in range(count) if not totals[i])
        updated = [(1 - damping + damping * dangling) * value for value in reset]
        for left, neighbors in adjacency.items():
            for right, weight in neighbors:
                updated[right] += damping * scores[left] * weight / totals[left]
        scores = updated
    _check_time(scope, now)
    return dict(zip(ids, scores[:len(ids)], strict=True))
