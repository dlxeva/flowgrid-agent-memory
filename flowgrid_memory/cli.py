"""Product entry-point wrapper around the internal implementation."""
from aml_retriever.product_cli import main

__all__ = ["main"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
