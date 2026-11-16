"""python -m riskplatform.dashboard (or riskplatform-dashboard): start the Streamlit dashboard.

Extra arguments go to `streamlit run`, e.g. --server.port 8502.
"""

import sys
from pathlib import Path

APP = Path(__file__).with_name("app.py")


def main() -> int:
    from streamlit.web import cli  # imported here: only the dashboard needs Streamlit

    sys.argv = ["streamlit", "run", str(APP), *sys.argv[1:]]
    return cli.main()


if __name__ == "__main__":
    sys.exit(main())
