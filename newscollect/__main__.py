import logging
import os

import uvicorn
from dotenv import find_dotenv, load_dotenv

from .config import load_config

# .env from the working directory (or a parent), like config.toml: ANTHROPIC_API_KEY etc. Existing env vars win.
load_dotenv(find_dotenv(usecwd=True))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
cfg = load_config()
uvicorn.run("newscollect.web:app_factory", factory=True, host=cfg.host, port=cfg.port)
