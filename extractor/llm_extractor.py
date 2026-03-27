import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

from extractor.validator import RateCon

load_dotenv()

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


def extract(file_path: str) -> RateCon:
    """
    Reads a PDF or image ratecon file and returns a validated RateCon object.
    """
    from extractor.pdf_reader import read_file

    mime_type, file_bytes = read_file(file_path)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Content(
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

    return RateCon.model_validate_json(response.text)
