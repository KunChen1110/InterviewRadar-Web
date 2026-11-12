"""Use wholly synthetic questions when exercising curated-corpus behaviour.

The public repository does not ship the production interview corpus. Tests
point at a small fixture instead of silently depending on a private checkout.
"""

from pathlib import Path

from backend import knowledge_plan


knowledge_plan.KNOWLEDGE_DIR = str(Path(__file__).parent / "fixtures" / "demo_knowledge")
