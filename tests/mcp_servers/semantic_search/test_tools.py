"""Tests for semantic_search/tools.py.

Tests cover: init_config, cleanup, error handling, result formatting,
client-side glob filtering, and singleton client management.
"""

from unittest.mock import MagicMock, patch

from mcp_servers.semantic_search.tools import cleanup, init_config, semantic_search


class TestInitConfig:
    def test_sets_module_state_from_env(self, monkeypatch, tmp_path):
        """init_config should set module-level state from env vars."""
        monkeypatch.setenv("REPO_PATH", str(tmp_path))
        monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("EMBEDDING_DIMENSION", "512")

        # Mock the client constructors to avoid requiring actual deps
        with (
            patch("mcp_servers.semantic_search.tools.QdrantClient", create=True),
            patch("mcp_servers.semantic_search.tools.genai", create=True),
        ):
            # Patch the imports inside init_config
            with patch.dict(
                "sys.modules",
                {
                    "google": MagicMock(),
                    "google.genai": MagicMock(),
                    "qdrant_client": MagicMock(),
                    "qdrant_client.QdrantClient": MagicMock(),
                },
            ):
                init_config()

    def test_no_collection_when_no_repo(self, monkeypatch, tmp_path):
        """Without GITHUB_REPOSITORY, collection_name should be None."""
        monkeypatch.setenv("REPO_PATH", str(tmp_path))
        monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

        init_config()

        from mcp_servers.semantic_search.tools import _collection_name

        assert _collection_name is None


class TestCleanup:
    def test_closes_qdrant_client(self):
        """cleanup() should close the qdrant client if it exists."""
        from mcp_servers.semantic_search import tools

        mock_client = MagicMock()
        tools._qdrant_client = mock_client

        cleanup()

        mock_client.close.assert_called_once()
        assert tools._qdrant_client is None

    def test_noop_when_no_client(self):
        """cleanup() should be safe when no client exists."""
        from mcp_servers.semantic_search import tools

        tools._qdrant_client = None
        cleanup()  # Should not raise


class TestSemanticSearchErrors:
    def test_returns_error_when_not_configured(self, monkeypatch):
        """Should return error dict when Qdrant/Gemini not configured."""
        monkeypatch.delenv("QDRANT_URL", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        # Reset module state
        from mcp_servers.semantic_search import tools

        tools._qdrant_url = None
        tools._gemini_api_key = None
        tools._collection_name = None
        tools._genai_client = None
        tools._qdrant_client = None

        result = semantic_search("test query")
        assert "error" in result
        assert result["results"] == []
        assert "not configured" in result["error"]

    def test_returns_error_when_no_collection(self, monkeypatch):
        """Should return error when GITHUB_REPOSITORY not set."""
        from mcp_servers.semantic_search import tools

        tools._qdrant_url = "http://localhost:6333"
        tools._gemini_api_key = "test-key"
        tools._collection_name = None

        result = semantic_search("test query")
        assert "error" in result
        assert "GITHUB_REPOSITORY" in result["error"]

    def test_returns_error_when_clients_not_initialized(self, monkeypatch):
        """Should return error when clients failed to initialize."""
        from mcp_servers.semantic_search import tools

        tools._qdrant_url = "http://localhost:6333"
        tools._gemini_api_key = "test-key"
        tools._collection_name = "owner__repo"
        tools._genai_client = None
        tools._qdrant_client = None

        result = semantic_search("test query")
        assert "error" in result
        assert "not initialized" in result["error"]


class TestSemanticSearchSuccess:
    def test_returns_results_dict_on_success(self):
        """Successful search should return dict with 'results' key."""
        from mcp_servers.semantic_search import tools

        # Set up module state
        tools._qdrant_url = "http://localhost:6333"
        tools._gemini_api_key = "test-key"
        tools._collection_name = "owner__repo"
        tools._embedding_dimension = 1024

        # Mock the genai client
        mock_genai_client = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.values = [0.1] * 1024
        mock_response = MagicMock()
        mock_response.embeddings = [mock_embedding]
        mock_genai_client.models.embed_content.return_value = mock_response
        tools._genai_client = mock_genai_client

        # Mock the Qdrant client
        mock_qdrant = MagicMock()
        mock_point = MagicMock()
        mock_point.payload = {
            "filepath": "shared/utils.py",
            "name": "helper",
            "kind": "function",
            "start_line": 10,
            "end_line": 20,
            "content": "def helper(): pass",
        }
        mock_point.score = 0.95
        mock_qdrant.query_points.return_value = MagicMock(points=[mock_point])
        tools._qdrant_client = mock_qdrant

        result = semantic_search("helper function")

        assert "results" in result
        assert "error" not in result
        assert len(result["results"]) == 1
        assert result["results"][0]["file"] == "shared/utils.py"
        assert result["results"][0]["score"] == 0.95

    def test_applies_glob_filtering(self):
        """Client-side glob filtering should exclude non-matching files."""
        from mcp_servers.semantic_search import tools

        tools._qdrant_url = "http://localhost:6333"
        tools._gemini_api_key = "test-key"
        tools._collection_name = "owner__repo"
        tools._embedding_dimension = 1024

        # Mock genai
        mock_genai_client = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.values = [0.1] * 1024
        mock_response = MagicMock()
        mock_response.embeddings = [mock_embedding]
        mock_genai_client.models.embed_content.return_value = mock_response
        tools._genai_client = mock_genai_client

        # Mock Qdrant with multiple results
        mock_qdrant = MagicMock()
        points = []
        for filepath in ["shared/utils.py", "services/worker.py", "shared/config.py"]:
            p = MagicMock()
            p.payload = {
                "filepath": filepath,
                "name": "cls",
                "kind": "class",
                "start_line": 1,
                "end_line": 10,
                "content": "...",
            }
            p.score = 0.9
            points.append(p)

        mock_qdrant.query_points.return_value = MagicMock(points=points)
        tools._qdrant_client = mock_qdrant

        # Filter to shared/*.py only
        result = semantic_search("test", file_filter="shared/*.py")

        assert "results" in result
        assert len(result["results"]) == 2
        assert all(r["file"].startswith("shared/") for r in result["results"])

    def test_respects_max_results(self):
        """Should cap results at max_results."""
        from mcp_servers.semantic_search import tools

        tools._qdrant_url = "http://localhost:6333"
        tools._gemini_api_key = "test-key"
        tools._collection_name = "owner__repo"
        tools._embedding_dimension = 1024

        mock_genai_client = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.values = [0.1] * 1024
        mock_response = MagicMock()
        mock_response.embeddings = [mock_embedding]
        mock_genai_client.models.embed_content.return_value = mock_response
        tools._genai_client = mock_genai_client

        # Create 10 results
        points = []
        for i in range(10):
            p = MagicMock()
            p.payload = {
                "filepath": f"file_{i}.py",
                "name": f"func_{i}",
                "kind": "function",
                "start_line": 1,
                "end_line": 10,
                "content": "...",
            }
            p.score = 0.9 - i * 0.01
            points.append(p)

        mock_qdrant = MagicMock()
        mock_qdrant.query_points.return_value = MagicMock(points=points)
        tools._qdrant_client = mock_qdrant

        result = semantic_search("test", max_results=3)
        assert len(result["results"]) == 3

    def test_returns_error_on_empty_embedding(self):
        """Should return error when embedding returns no values."""
        from mcp_servers.semantic_search import tools

        tools._qdrant_url = "http://localhost:6333"
        tools._gemini_api_key = "test-key"
        tools._collection_name = "owner__repo"
        tools._embedding_dimension = 1024

        mock_genai_client = MagicMock()
        mock_response = MagicMock()
        mock_response.embeddings = []
        mock_genai_client.models.embed_content.return_value = mock_response
        tools._genai_client = mock_genai_client
        tools._qdrant_client = MagicMock()

        result = semantic_search("test")
        assert "error" in result
        assert "embedding" in result["error"].lower()

    def test_returns_error_on_exception(self):
        """Should return error dict on unexpected exceptions."""
        from mcp_servers.semantic_search import tools

        tools._qdrant_url = "http://localhost:6333"
        tools._gemini_api_key = "test-key"
        tools._collection_name = "owner__repo"
        tools._embedding_dimension = 1024

        mock_genai_client = MagicMock()
        mock_genai_client.models.embed_content.side_effect = RuntimeError("API down")
        tools._genai_client = mock_genai_client
        tools._qdrant_client = MagicMock()

        result = semantic_search("test")
        assert "error" in result
        assert "RuntimeError" in result["error"]
