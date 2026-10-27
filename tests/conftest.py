import os
import sys
import tempfile

_T = tempfile.mkdtemp(prefix="meelees-toets-")
os.environ["MEELEES_STAAT"] = os.path.join(_T, "staat")
os.environ["MEELEES_GEHEIM"] = os.path.join(_T, "geheim")
os.environ["MEELEES_WERK"] = os.path.join(_T, "werk")
os.environ["MEELEES_SOCEV_HASHES"] = "9d1b2bd8a3f38b8d1d0a2e8d1c4a9c3cbbd9e4b5f0f0b6f3a0ae8c4b9e2d7f10"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
