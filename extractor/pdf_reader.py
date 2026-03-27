from pathlib import Path

SUPPORTED_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".webp": "image/webp",
}


def read_file(file_path: str) -> tuple[str, bytes]:
    """
    Returns (mime_type, file_bytes) for a given PDF or image file.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix not in SUPPORTED_TYPES:
        raise ValueError(
            f"Unsupported file type: '{suffix}'. "
            f"Supported types: {', '.join(SUPPORTED_TYPES.keys())}"
        )

    mime_type = SUPPORTED_TYPES[suffix]

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    return mime_type, file_bytes
