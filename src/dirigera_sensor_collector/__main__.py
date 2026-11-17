"""Allow running the collector with ``python -m dirigera_sensor_collector``."""

from .service import main

raise SystemExit(main())
