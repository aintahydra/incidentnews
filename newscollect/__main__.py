import logging
import os
import sys

import uvicorn
from dotenv import find_dotenv, load_dotenv

from .config import load_config

# .env from the working directory (or a parent), like config.toml: ANTHROPIC_API_KEY etc. Existing env vars win.
load_dotenv(find_dotenv(usecwd=True))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
cfg = load_config()
if sys.argv[1:2] == ["retag"]:  # python -m newscollect retag [id ...]: re-tag stored incidents, then exit
    from . import db, retag
    ids = [int(x) for x in sys.argv[2:]]
    print(retag.retag(db.connect(cfg.database), cfg, ids or None))
else:
    uvicorn.run("newscollect.web:app_factory", factory=True, host=cfg.host, port=cfg.port)
