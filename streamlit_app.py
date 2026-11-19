"""Entry point of the public demo dashboard on Streamlit Community Cloud (Phase 10).

Community Cloud runs `streamlit run streamlit_app.py` from the repository root, after installing
requirements.txt (this project, with its dashboard extra). Demo mode builds the default synthetic
bank with the real pipeline once per server start (riskplatform.dashboard.demo) and shows that
run: no AWS access and no secrets are needed.

Locally, the same demo:  streamlit run streamlit_app.py
The dashboard on your own data/ (or S3) instead:  riskplatform-dashboard
"""

import os

os.environ.setdefault("RISKPLATFORM_DEMO", "1")
os.environ.setdefault("RISKPLATFORM_STORAGE_BACKEND", "local")  # never reach for S3 here

from riskplatform.dashboard.app import main  # imported after the environment is set

main()
