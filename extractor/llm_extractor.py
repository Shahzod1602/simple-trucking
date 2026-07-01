import hashlib
import json
import logging
import os
import time
from collections import OrderedDict

from google import genai
from google.genai import types
from dotenv import load_dotenv

from extractor.validator import RateCon

load_dotenv()
logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Raised when extraction cannot complete (quota, timeout, transient error,
    or empty model output). Carries a user-safe message — map to HTTP 503 at the
    API layer instead of leaking the raw provider exception."""


EXTRACTION_PROMPT = """You are an expert logistics document analyst specializing in rate confirmations (ratecons).

Extract ALL the following fields from this rate confirmation document.

RULES:
- broker: Broker or shipper company name
- carrier: Carrier or trucking company name
- total_rate_usd: Total rate/pay amount, always include $ sign (e.g. "$1,675.00")
- load_number: Load, order, or pro number assigned by broker
- customer_ref: Customer or shipper reference number, null if not found
- commodity: Description of goods being shipped
- weight: Gross weight in pounds, numbers only (e.g. "26000")
- miles: Total trip miles/distance as a number string (e.g. "494"), null if not found
- stops: List of ALL pickup and delivery stops in order
  - address_line_1: Facility or company name at this location (if shown)
  - address_line_2: Street address
  - city: City name in UPPERCASE
  - state: 2-letter state abbreviation in UPPERCASE
  - zip: ZIP code (5 or 9 digits)
  - country: "USA"
  - date: Date and time in format MM/DD/YYYY HHMM (24h), e.g. "11/11/2025 1300"
  - type: "pickup" or "delivery"
  - reference: PU number, DEL number, PRO number, or stop reference number, null if not found

IMPORTANT:
- Return ONLY valid JSON, no explanations or markdown.
- If a field is not found in the document, use null.
- Do not guess or invent values — only extract what is explicitly written.
- Extract ALL stops, not just the first one.
"""

REQUEST_TIMEOUT_MS = 60_000  # 60s cap on the Gemini call
MAX_RETRIES = 3
_TRANSIENT_MARKERS = (
    "429", "resource_exhausted", "rate limit", "quota",
    "500", "502", "503", "unavailable", "deadline", "timeout",
)

# Small in-process LRU: identical file bytes → prior result. Avoids paying for a
# re-extraction when the same PDF is uploaded again seconds later.
_CACHE_MAX = 64
_result_cache: "OrderedDict[str, RateCon]" = OrderedDict()


def _is_transient(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(m in s for m in _TRANSIENT_MARKERS)


def _is_quota(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "resource_exhausted" in s or "quota" in s


def _stop_address_string(stop) -> str:
    a = stop.address
    parts = [a.address_line_2, a.city, a.state, a.zip, a.country]
    return ", ".join(p for p in parts if p)


def _fill_missing_miles(ratecon: RateCon, api_key: str | None) -> None:
    if ratecon.miles:
        return
    try:
        from routing import calculate_total_miles
        addresses = [_stop_address_string(s) for s in ratecon.stops]
        miles = calculate_total_miles(addresses, api_key)
        if miles:
            ratecon.miles = str(miles)
    except Exception as e:
        logger.warning("Miles fallback calc failed: %s", e)


def _use_vertex() -> bool:
    """True when extraction should go through Vertex AI (ADC credentials) rather
    than the Gemini Developer API key."""
    flag = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    # Auto: use Vertex when an ADC credentials file is configured.
    return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip())


def _vertex_project() -> str | None:
    """Resolve the Vertex project. Explicit GOOGLE_CLOUD_PROJECT wins; otherwise
    read quota_project_id from the ADC key file, so swapping the key file swaps
    the project with no code change."""
    proj = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if proj:
        return proj
    cred = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if cred and os.path.isfile(cred):
        try:
            with open(cred) as fh:
                data = json.load(fh)
            return data.get("quota_project_id") or data.get("project_id")
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not read Vertex project from ADC file: %s", e)
    return None


def extraction_configured() -> bool:
    """Whether extraction can run at all — either Vertex (ADC) or a Gemini API key."""
    if _use_vertex():
        return bool(_vertex_project())
    return bool(os.environ.get("GEMINI_API_KEY"))


def _build_client() -> genai.Client:
    """Build the genai client: Vertex AI (ADC) when configured, else the Gemini
    Developer API key. Raises ExtractionError (→ 503) when neither is set."""
    http_opts = types.HttpOptions(timeout=REQUEST_TIMEOUT_MS)
    if _use_vertex():
        project = _vertex_project()
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip() or "global"
        if not project:
            raise ExtractionError("Extraction is not configured (Vertex project not resolved).")
        return genai.Client(vertexai=True, project=project, location=location, http_options=http_opts)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ExtractionError("Extraction is not configured (no Vertex credentials or API key).")
    return genai.Client(api_key=api_key, http_options=http_opts)


def extract(file_path: str, gmaps_api_key: str | None = None) -> RateCon:
    """
    Reads a PDF or image ratecon file and returns a validated RateCon object.

    Hardened against real-world failure modes:
      - identical files are served from a small in-process cache (cost control),
      - the Gemini call has a hard timeout and bounded retries with backoff on
        transient/quota errors,
      - empty model output and quota exhaustion raise ExtractionError (→ 503)
        instead of an opaque 500.
    If the extracted miles field is empty, falls back to calculating total
    driving miles via Google Maps (with OSRM fallback) from the stops.
    """
    from extractor.pdf_reader import read_file

    mime_type, file_bytes = read_file(file_path)

    cache_key = hashlib.sha256(file_bytes).hexdigest()
    cached = _result_cache.get(cache_key)
    if cached is not None:
        _result_cache.move_to_end(cache_key)
        return cached.model_copy(deep=True)

    client = _build_client()

    response = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Content(
                        role="user",  # Vertex AI requires an explicit role (user/model)
                        parts=[
                            types.Part(text=EXTRACTION_PROMPT),
                            types.Part(
                                inline_data=types.Blob(
                                    mime_type=mime_type,
                                    data=file_bytes,
                                ),
                            ),
                        ]
                    )
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=RateCon.model_json_schema(),
                    temperature=0.0,
                ),
            )
            break
        except Exception as exc:
            is_last = attempt >= MAX_RETRIES - 1
            if _is_transient(exc) and not is_last:
                time.sleep(0.8 * (2 ** attempt))
                continue
            if _is_quota(exc):
                raise ExtractionError(
                    "Extraction is temporarily unavailable (AI quota reached). "
                    "Please try again shortly."
                ) from exc
            logger.warning("Gemini extraction failed: %s", exc)
            raise ExtractionError(
                "Could not read this document. Please try again with a clearer file."
            ) from exc

    if response is None or not getattr(response, "text", None):
        raise ExtractionError(
            "The document could not be read (no data returned). Try a clearer scan."
        )

    ratecon = RateCon.model_validate_json(response.text)
    _fill_missing_miles(ratecon, gmaps_api_key)

    _result_cache[cache_key] = ratecon
    _result_cache.move_to_end(cache_key)
    if len(_result_cache) > _CACHE_MAX:
        _result_cache.popitem(last=False)
    return ratecon.model_copy(deep=True)
