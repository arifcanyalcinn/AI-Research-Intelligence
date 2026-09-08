"""
Embedding backend package.

Plugin discovery relies on ``BaseEmbedder.__subclasses__()``, which is
populated by the explicit imports below (SDS §1.2, AD-02). ``EmbeddingRegistry``
must therefore be constructed *after* this module has been imported — which
happens automatically for anyone importing ``arip.backends.embeddings.registry``.

To add a backend:
  1. Create ``arip/backends/embeddings/<name>.py`` with a direct
     ``BaseEmbedder`` subclass.
  2. Add one import line here — that is the entire registration step.

D-004 applies: ``__subclasses__()`` does not recurse, so each backend subclasses
``BaseEmbedder`` directly. `StubEmbedder` and `SentenceTransformersBackend`
share no intermediate base, and the structural duplication between them is
accepted and intentional.

Neither module imports ``numpy`` or ``sentence_transformers`` at module level:
both arrive with the optional ``embedding`` extra, and these imports run
whenever the package is imported.
"""

from .sentence_transformers_backend import SentenceTransformersBackend  # noqa: F401
from .stub_backend import StubEmbedder  # noqa: F401
