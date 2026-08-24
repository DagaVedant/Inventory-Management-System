"""Entry point for serverless platforms that import a module level ``app``.

This is a thin adapter, not a second way to run the project: it hands back the
same WSGI callable gunicorn serves everywhere else, so there is no chance of the
two drifting apart. The path insert is needed because the function is imported
from inside api/ rather than from the project root.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.wsgi import application  # noqa: E402

app = application
