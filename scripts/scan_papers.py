from __future__ import annotations

from backend.indexer import scan_papers


if __name__ == "__main__":
    result = scan_papers()
    print(result)

