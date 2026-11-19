"""
LLM backend package (SDS §5.9).

Plugin discovery relies on ``BaseLLM.__subclasses__()``, which is populated by
the explicit imports below (SDS §1.2, AD-02). ``LLMRegistry`` must therefore be
constructed after this module has been imported, which happens automatically
for anyone importing ``arip.backends.llm.registry``.

To add a backend:
  1. Create ``arip/backends/llm/<name>.py`` with a direct ``BaseLLM`` subclass.
  2. Add one import line here; that is the entire registration step.

D-004 applies: ``__subclasses__()`` does not recurse, so each backend subclasses
``BaseLLM`` directly.

Neither module imports ``torch`` or ``transformers`` at module level: both
arrive with the optional ``llm`` extra, and these imports run whenever the
package is imported.
"""

from .stub_backend import StubLLM  # noqa: F401
from .transformers_backend import TransformersBackend  # noqa: F401
