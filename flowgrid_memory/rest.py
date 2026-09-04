"""Product entry-point wrapper around the internal implementation."""
from aml_retriever.rest_v1 import main

__all__ = ["main"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
