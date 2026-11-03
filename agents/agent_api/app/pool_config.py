"""Explicit connection budgets for each independent Python pool."""
import os


def pool_sizes(kind: str, minimum: int, maximum: int) -> dict[str, int]:
    low = int(os.getenv(f"JARVIS_DB_{kind}_MIN", str(minimum)))
    high = int(os.getenv(f"JARVIS_DB_{kind}_MAX", str(maximum)))
    if low < 0 or high < 1 or low > high:
        raise ValueError(f"Invalid JARVIS_DB_{kind}_MIN/MAX connection budget")
    return {"min_size": low, "max_size": high}
