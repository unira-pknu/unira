"""Shared model configuration and normalized sentence embeddings for batch tools."""
import math

DEFAULT_SENTENCE_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EXPECTED_DIMENSIONS = 384

# Second embedding, stored in <family>_chunks_e5 (migration 021). Measured on 48k of
# our chunks (eval/tools/embedding_benchmark.py, 2026-11-13): the gold document is in
# the top 10 for 69% of questions vs 6% with the model above. e5 was trained with
# these prefixes; leaving them out lowers its scores.
E5_MODEL = "intfloat/multilingual-e5-base"
E5_DIMENSIONS = 768
E5_PASSAGE_PREFIX = "passage: "
E5_QUERY_PREFIX = "query: "


def load_sentence_model(model_name=DEFAULT_SENTENCE_MODEL, *, device=None):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_name, **({"device": device} if device else {}))


def encode_normalized(model, texts, *, batch_size=32):
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False, batch_size=batch_size)


def validate_vectors(vectors, count, dimensions):
    if dimensions <= 0 or len(vectors) != count:
        raise ValueError("Embedding count or dimensions are invalid")
    if any(len(row) != dimensions or any(not math.isfinite(float(v)) for v in row) for row in vectors):
        raise ValueError("Embedding dimension mismatch or non-finite value")


class SentenceTransformerEmbedder:
    def __init__(self, model_name=DEFAULT_SENTENCE_MODEL):
        self.model_name = model_name
        self.model = load_sentence_model(model_name)
        self.name = f"sentence-transformers:{model_name}"
        self.dimensions = int(self.model.get_sentence_embedding_dimension() or 0)
        if self.dimensions <= 0:
            raise ValueError("Model did not report a valid embedding dimension")

    def embed_texts(self, texts):
        vectors = encode_normalized(self.model, texts)
        validate_vectors(vectors, len(texts), self.dimensions)
        return [[round(float(value), 8) for value in row] for row in vectors]
