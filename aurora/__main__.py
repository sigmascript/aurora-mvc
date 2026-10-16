"""Allow `python -m aurora` as an alternative to the console script."""
from .cli import main

raise SystemExit(main())
