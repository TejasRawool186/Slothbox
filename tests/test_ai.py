"""Tests for AI engine parsing, vector similarity, and failure handling."""

import numpy as np
import pytest
from src.ai import AIEngine, OllamaProcessingError


def test_parse_summary_and_tags():
    raw_output = """SUMMARY: Comprehensive tutorial on building Retrieval-Augmented Generation systems using local LLMs.
TAGS: ai, rag, python, llm, local
"""
    summary, tags = AIEngine._parse_summary_and_tags(raw_output)
    assert "Comprehensive tutorial on building Retrieval-Augmented Generation" in summary
    assert tags == ["ai", "rag", "python", "llm", "local"]


def test_parse_summary_and_tags_with_hashtags_and_brackets():
    raw_output = """SUMMARY: An image showing a cute sloth sleeping on a tree branch.
TAGS: #sloth, [wildlife], "nature", 'sleepy'
"""
    summary, tags = AIEngine._parse_summary_and_tags(raw_output)
    assert "sloth sleeping" in summary
    assert tags == ["sloth", "wildlife", "nature", "sleepy"]


def test_cosine_similarity_ranking():
    engine = AIEngine()

    # Query vector pointing mostly along X axis
    query_vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    # Stored embeddings:
    # Item 1: Exactly parallel -> cos = 1.0
    # Item 2: 45 degrees -> cos ≈ 0.707
    # Item 3: Orthogonal -> cos = 0.0
    # Item 4: Opposite direction -> cos = -1.0
    stored = [
        (1, np.array([1.0, 0.0, 0.0], dtype=np.float32)),
        (2, np.array([1.0, 1.0, 0.0], dtype=np.float32)),
        (3, np.array([0.0, 1.0, 0.0], dtype=np.float32)),
        (4, np.array([-1.0, 0.0, 0.0], dtype=np.float32)),
    ]

    top_matches = engine.compute_top_similar(query_vec, stored, top_k=2)
    assert len(top_matches) == 2
    assert top_matches[0][0] == 1  # item 1 is closest
    assert pytest.approx(top_matches[0][1], 0.001) == 1.0

    assert top_matches[1][0] == 2  # item 2 is next
    assert pytest.approx(top_matches[1][1], 0.001) == 0.7071


@pytest.mark.asyncio
async def test_ollama_offline_raises_error():
    """Verify that when Ollama is unreachable, an explicit OllamaProcessingError is raised."""
    # Use non-existent port to simulate offline Ollama
    offline_engine = AIEngine(base_url="http://127.0.0.1:59999")
    
    with pytest.raises(OllamaProcessingError, match="Ollama"):
        await offline_engine.generate_summary_and_tags("Test content")

    with pytest.raises(OllamaProcessingError, match="Ollama"):
        await offline_engine.generate_embedding("Test query")
