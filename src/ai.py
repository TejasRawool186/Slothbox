"""Local Ollama AI engine for summarization, vision description, and vector embeddings."""

import re
from pathlib import Path
from typing import Any, Optional
import httpx
import numpy as np
import ollama


class OllamaProcessingError(Exception):
    """Raised when Ollama is unreachable, model crashes, or inference fails."""


class AIEngine:
    """Interface to local Ollama models with zero synthetic fallback."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        llm_model: str = "gemma2:2b",
        embed_model: str = "nomic-embed-text",
        vision_model: str = "moondream",
        rag_model: str = "qwen3:4b",
    ):
        self.base_url = base_url.rstrip("/")
        self.llm_model = llm_model
        self.embed_model = embed_model
        self.vision_model = vision_model
        self.rag_model = rag_model
        self.client = ollama.AsyncClient(host=self.base_url)

    async def check_health(self) -> tuple[bool, str]:
        """Verify Ollama connectivity and model availability."""
        try:
            models_response = await self.client.list()
            available = [m.model for m in models_response.models]
            return True, f"Ollama online. Available models: {', '.join(available) if available else 'None'}"
        except Exception as exc:
            return False, f"Ollama not reachable at {self.base_url}: {exc}"

    async def generate_summary_and_tags(
        self, content: str, content_type: str = "text"
    ) -> tuple[str, list[str]]:
        """
        Generate a one-line summary and 3-5 tags using the local LLM.
        Strictly raises OllamaProcessingError if Ollama fails.
        """
        if not content.strip():
            raise OllamaProcessingError("Content is empty; cannot generate summary.")

        # Truncate overly long text to avoid blowing local context limit
        truncated_content = content[:4000]

        prompt = f"""You are Slothbox, a local knowledge archivist.
Analyze the following {content_type} content.
Provide your response in EXACTLY this format, with no other conversational text:

SUMMARY: <one concise, informative sentence summarizing the core value or topic>
TAGS: <3 to 5 lowercase comma-separated tags, no hashtags>

Content to analyze:
{truncated_content}
"""

        try:
            response = await self.client.chat(
                model=self.llm_model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.2},
            )
            raw_text = response.message.content.strip()
        except Exception as exc:
            raise OllamaProcessingError(f"Ollama LLM call failed: {exc}") from exc

        summary, tags = self._parse_summary_and_tags(raw_text)
        if not summary:
            raise OllamaProcessingError(
                f"LLM produced unparseable output: {raw_text[:200]}"
            )

        return summary, tags

    async def describe_image(
        self, image_path: str | Path
    ) -> tuple[str, list[str]]:
        """
        Analyze an image file using Ollama vision model to produce summary and tags.
        """
        path = Path(image_path)
        if not path.exists():
            raise OllamaProcessingError(f"Image file not found at {path}")

        prompt = """Analyze this image in detail.
Provide your response in EXACTLY this format, with no introductory text:

SUMMARY: <one concise, descriptive sentence explaining what is in the image>
TAGS: <3 to 5 lowercase comma-separated tags describing subjects and colors>
"""

        try:
            response = await self.client.chat(
                model=self.vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [str(path.resolve())],
                    }
                ],
                options={"temperature": 0.2},
            )
            raw_text = response.message.content.strip()
        except Exception as exc:
            raise OllamaProcessingError(f"Ollama Vision call failed: {exc}") from exc

        summary, tags = self._parse_summary_and_tags(raw_text)
        if not summary:
            raise OllamaProcessingError(
                f"Vision model produced unparseable output: {raw_text[:200]}"
            )

        return summary, tags

    async def generate_embedding(self, text: str) -> np.ndarray:
        """
        Generate dense vector embedding via local Ollama embed API.
        Strictly raises OllamaProcessingError on failure.
        """
        if not text.strip():
            raise OllamaProcessingError("Cannot generate embedding for empty text.")

        try:
            # ollama.AsyncClient.embed supports the modern /api/embed endpoint
            res = await self.client.embed(
                model=self.embed_model,
                input=text.strip(),
            )
            # Response may contain embeddings as list of vectors
            if hasattr(res, "embeddings") and res.embeddings:
                raw_vector = res.embeddings[0]
            elif isinstance(res, dict) and "embeddings" in res:
                raw_vector = res["embeddings"][0]
            else:
                raise OllamaProcessingError(f"Unexpected embedding response format: {res}")

            vector = np.array(raw_vector, dtype=np.float32)
            # Normalize vector for cosine similarity
            norm = np.linalg.norm(vector)
            if norm > 0:
                vector = vector / norm
            return vector
        except Exception as exc:
            raise OllamaProcessingError(
                f"Ollama embedding model '{self.embed_model}' failed: {exc}"
            ) from exc

    @staticmethod
    def compute_top_similar(
        query_vector: np.ndarray,
        stored_embeddings: list[tuple[int, np.ndarray]],
        top_k: int = 3,
        min_score: float = 0.0,
    ) -> list[tuple[int, float]]:
        """
        Compute cosine similarity between query_vector and stored embeddings.
        Returns top_k matches: list of (item_id, similarity_score) sorted descending.
        """
        if not stored_embeddings:
            return []

        query_norm = np.linalg.norm(query_vector)
        if query_norm == 0:
            return []

        normalized_query = query_vector / query_norm
        scored: list[tuple[int, float]] = []

        for item_id, vec in stored_embeddings:
            v_norm = np.linalg.norm(vec)
            if v_norm == 0:
                continue
            # Cosine similarity
            score = float(np.dot(normalized_query, vec) / v_norm)
            if score >= min_score:
                scored.append((item_id, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _parse_summary_and_tags(text: str) -> tuple[str, list[str]]:
        """Helper to extract SUMMARY and TAGS from LLM response."""
        summary = ""
        tags: list[str] = []

        # Look for SUMMARY: line
        summary_match = re.search(
            r"SUMMARY:\s*(.+?)(?=\nTAGS:|\n\n|$)", text, re.IGNORECASE | re.DOTALL
        )
        if summary_match:
            summary = summary_match.group(1).strip().replace("\n", " ")

        # Look for TAGS: line
        tags_match = re.search(r"TAGS:\s*(.+?)$", text, re.IGNORECASE | re.DOTALL)
        if tags_match:
            raw_tags = tags_match.group(1).strip()
            # Split by comma or semicolon
            for t in re.split(r"[,;]+", raw_tags):
                cleaned = re.sub(r"[#\[\]\'\"]", "", t).strip().lower()
                if cleaned and len(cleaned) <= 30:
                    tags.append(cleaned)

        # Fallback parsing if explicit prefix was omitted
        if not summary and text:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                summary = lines[0]
                if len(lines) > 1 and "tag" in lines[1].lower():
                    for t in re.split(r"[,;]+", lines[1]):
                        cleaned = re.sub(r"[#\[\]\'\"]", "", t).strip().lower()
                        if cleaned and len(cleaned) <= 30:
                            tags.append(cleaned)

        # Limit tags to 5
        tags = tags[:5]
        return summary, tags

    async def answer_question(
        self, question: str, context_items: list[dict[str, Any]]
    ) -> str:
        """
        Synthesize an answer using local RAG reasoning (e.g. qwen3:4b or fallback to llm_model).
        """
        if not context_items:
            return "No relevant notes or items found in your stash to answer this question."

        context_blocks = []
        for it in context_items:
            item_id = it.get("id")
            c_type = it.get("content_type", "item")
            summary = it.get("summary") or it.get("raw_content", "")[:200]
            tags = " ".join([f"#{t}" for t in it.get("tags", [])])
            full_text = it.get("full_text") or ""
            text_snippet = full_text[:600] if full_text else it.get("raw_content", "")[:300]
            context_blocks.append(
                f"[Item #{item_id} ({c_type})]\n"
                f"Summary: {summary}\n"
                f"Tags: {tags}\n"
                f"Content: {text_snippet}"
            )

        context_str = "\n\n".join(context_blocks)
        prompt = f"""You are Slothbox, an intelligent second-brain assistant.
Answer the user's question accurately using ONLY the saved context items below.
Cite the relevant Item IDs (e.g., [Item #2]) when answering.
If the answer is not contained in the context, say so politely.

CONTEXT ITEMS:
{context_str}

USER QUESTION:
{question}
"""
        model_to_use = self.rag_model or self.llm_model

        try:
            response = await self.client.chat(
                model=model_to_use,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.3},
            )
            return response.message.content.strip()
        except Exception:
            # Fallback to standard LLM if specific RAG model is not pulled
            try:
                response = await self.client.chat(
                    model=self.llm_model,
                    messages=[{"role": "user", "content": prompt}],
                    options={"temperature": 0.3},
                )
                return response.message.content.strip()
            except Exception as exc:
                raise OllamaProcessingError(f"RAG reasoning failed: {exc}") from exc

    async def text_to_speech(
        self, text: str, api_key: str, voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    ) -> Optional[bytes]:
        """
        Generate high-quality speech audio from text using ElevenLabs API.
        """
        if not api_key:
            return None

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        data = {
            "text": text[:1000],
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.8,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(url, headers=headers, json=data)
                if res.status_code == 200:
                    return res.content
                return None
        except Exception:
            return None
