"""PDF + AI parsing for the runsheet upload pipeline.

  pdf.py       — extract_pdf_text (pdfplumber wrapper)
  duration.py  — _TIME_RE, _DURATION_RE, _extract_*
  ai.py        — DEFAULT_PROMPT, prompt assembly, JSON cleanup of model output
"""

from .ai import (
    DEFAULT_PROMPT,
    LIBRARY_CONTEXT_ADDENDUM,
    LIBRARY_NAMES_MAX,
    SERVICE_MATE_CUE_ADDENDUM,
    assemble_prompt,
    parse_ai_response,
)
from .duration import (
    _DURATION_RE,
    _TIME_RE,
    _extract_duration_min,
    _extract_time_str,
)
from .pdf import extract_pdf_text
