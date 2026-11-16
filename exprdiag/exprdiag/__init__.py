"""ExprDiag：Expression Engine 的只读诊断部分。

只回答「哪一层在拒」，不负责让被拒内容通过。
"""

from .compat import (
    EXCLUDED_PROBES,
    PROBE_CLASSES,
    MockProvider,
    OpenAICompatProvider,
    classify_provider_result,
    render_matrix,
    run_matrix,
)
from .inspect import (
    DiagnosisReport,
    LayerFinding,
    classify_refusal,
    detect_prompt_conflicts,
    diagnose,
    inspect_persona_layers,
    render_markdown,
    scan_text_for_rules,
)

__all__ = [
    "EXCLUDED_PROBES",
    "PROBE_CLASSES",
    "DiagnosisReport",
    "LayerFinding",
    "MockProvider",
    "OpenAICompatProvider",
    "classify_provider_result",
    "classify_refusal",
    "detect_prompt_conflicts",
    "diagnose",
    "inspect_persona_layers",
    "render_markdown",
    "render_matrix",
    "run_matrix",
    "scan_text_for_rules",
]

__version__ = "0.1.0"
