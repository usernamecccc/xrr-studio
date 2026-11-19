"""把插件后端依赖装到 vendor/ 目录。

WGallop 自带 Python 只有标准库与 MCP SDK，没有 numpy/h5py。
插件后端（backend/core.py）启动时会把 vendor/ 加入 sys.path，
所以只需把依赖装到这个目录，不用改动宿主的 Python 环境。

用法：
    python tools/install_deps.py
    python tools/install_deps.py -i https://pypi.org/simple
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
VENDOR_DIR = PLUGIN_DIR / "vendor"

DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


def find_host_python() -> str:
    """优先用 WGallop 的随包 Python，其次当前解释器。"""
    override = os.environ.get("WGALLOP_PLUGIN_PYTHON")
    if override and Path(override).exists():
        return override

    runtimes = Path.home() / ".wgallop" / "runtimes" / "plugin-python"
    if runtimes.is_dir():
        candidates = sorted(runtimes.iterdir(), reverse=True)
        for child in candidates:
            for exe in (child / "python.exe", child / "bin" / "python3"):
                if exe.exists():
                    return str(exe)
    return sys.executable


def main() -> int:
    parser = argparse.ArgumentParser(description="安装插件后端依赖到 vendor/")
    parser.add_argument("-i", "--index-url", default=DEFAULT_INDEX, help="pip 源")
    parser.add_argument("--python", default=None, help="指定 Python 解释器")
    args = parser.parse_args()

    python = args.python or find_host_python()
    print(f"插件目录: {PLUGIN_DIR}")
    print(f"目标目录: {VENDOR_DIR}")
    print(f"解释器  : {python}")

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        python, "-m", "pip", "install",
        "--target", str(VENDOR_DIR),
        "--upgrade",
        "-i", args.index_url,
        "numpy", "h5py",
    ]
    print("执行:", " ".join(command))
    result = subprocess.run(command)
    if result.returncode != 0:
        print("\n安装失败。可尝试：")
        print("  1) 换源：python tools/install_deps.py -i https://pypi.org/simple")
        print("  2) 指定解释器：python tools/install_deps.py --python <python.exe>")
        return result.returncode

    print("\n完成。依赖已装到:", VENDOR_DIR)
    print("下一步：在 WGallop 里重新打开插件（或点一次「仿真」），后端即可加载。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

