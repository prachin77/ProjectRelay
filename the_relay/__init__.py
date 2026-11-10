"""The Relay — a research-grounded RAG assistant over three pinned arXiv papers.

Package layout
--------------
``config``       typed settings; every cost/quality knob in one place
``core``         request handling: normalisation, scope, quota, admission,
                 retries, and the pipeline that joins everything together
``rag``          ingestion, chunking, embeddings, hybrid retrieval, evidence
                 assessment, generation, citation validation
``cache``        Redis exact + semantic caches, the equivalence guard, and
                 version-aware keys
``providers``    the Groq adapter and the deterministic local stand-in
``eval``         the 15+ question evaluation set, grading, metrics
``experiments``  the real-service probe and the load-shape runners
``obs``          request / model-call / cache traces and the results CSV

The design rule that everything else follows: **at most one model call per
request, and only after caching and evidence checks have had their say.**
"""

__version__ = "0.1.0"
