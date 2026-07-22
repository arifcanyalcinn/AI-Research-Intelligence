"""
Source plugin package for ARIP.

Plugin discovery relies on BaseSource.__subclasses__(), which is populated
by the explicit imports below. The SourceRegistry must be constructed AFTER
this module has been imported (which happens automatically when any code does
``import arip.sources`` or ``from arip.sources.registry import SourceRegistry``).

To add a new source:
  1. Create arip/sources/<name>.py with a BaseSource subclass.
  2. Add one import line here — that is the entire registration step.
  3. Nothing else changes.

Import order is deterministic and visible in this file.
"""

# Sources are imported here so that their classes are registered as
# BaseSource subclasses before SourceRegistry.__init__ runs.
#
# Imports are added incrementally as each source batch is completed:
#   Batch 2  → ArXivSource
#   Batch 3  → HuggingFacePapersSource
#   Batch 4  → HuggingFaceModelsSource, HuggingFaceSpacesSource
#   Batch 5  → GitHubTrendingSource
#   Batch 6  → PapersWithCodeSource
