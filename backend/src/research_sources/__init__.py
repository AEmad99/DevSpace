"""Public surface of the research_sources package.

Importing this package also imports (and registers) the built-in sources.
The first import of any `Source` subclass module triggers its
`@registry.register` decorator, so callers should import the concrete
adapter module when they want to ensure it is loaded:

    from src.research_sources import registry          # bare registry
    from src.research_sources import internet          # registers InternetSource
    from src import research_sources.internet          # equivalent

Other adapters (folder, codebase, knowledge_base) will be added in M2+.
"""
from .base import Finding, LLMFn, Source, SourceRef
from .registry import SourceRegistry, registry

# Side-effect imports: importing the module registers the adapter.
from . import internet  # noqa: F401  -- registers InternetSource
from . import folder    # noqa: F401  -- registers FolderSource  (M2)
from . import codebase  # noqa: F401  -- registers CodebaseSource (M3)
from . import knowledge_base  # noqa: F401  -- registers KnowledgeBaseSource (M4)

__all__ = [
    "Finding",
    "LLMFn",
    "Source",
    "SourceRef",
    "SourceRegistry",
    "registry",
]
