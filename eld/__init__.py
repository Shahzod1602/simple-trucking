from .samsara import SamsaraClient
from .motive import MotiveClient
from .zippy import ZippyClient
from .evo import EvoClient


def get_client(provider: str, api_key: str, company: str | None = None, provider_token: str | None = None):
    if provider == "samsara":
        return SamsaraClient(api_key)
    elif provider == "motive":
        return MotiveClient(api_key)
    elif provider == "zippyeld":
        if not provider_token:
            raise ValueError("ZippyELD requires a Provider Token")
        if not company:
            raise ValueError("ZippyELD requires a USDOT number")
        return ZippyClient(api_key, provider_token, company)
    elif provider == "evoeld":
        if not provider_token:
            raise ValueError("EVO ELD requires a Provider Token")
        if not company:
            raise ValueError("EVO ELD requires a USDOT number")
        return EvoClient(api_key, provider_token, company)
    raise ValueError(f"Unknown ELD provider: {provider}")
