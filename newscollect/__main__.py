import logging
import os

import uvicorn

from .config import load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
cfg = load_config()
uvicorn.run("newscollect.web:app_factory", factory=True, host=cfg.host, port=cfg.port)
