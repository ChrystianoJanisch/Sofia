import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

import uvicorn

if __name__ == "_main_":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True, reload_dirs=["backend"])