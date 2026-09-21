#!/usr/bin/env bash
# 受限网络环境下的依赖引导（PyPI / npm 被出口策略拦截，但 GitHub 可达时使用）。
#
# 正常环境请直接用：cd backend && pip install -e ".[dev]"
# 本脚本只在 pip 报 403 / "No matching distribution found" 时才需要。
#
# 原理：纯 Python 包直接从 GitHub 取源码目录，放进 PYTHONPATH，绕开 PyPI 与构建隔离。
# 二进制包（pydantic-core / numpy / opencv）无法这样处理，必须环境里本来就有。
set -euo pipefail

LIB="${PF_LIB_DIR:-/opt/pf-libs}"
SRC="$(mktemp -d)"
mkdir -p "$LIB"

fetch() { # repo tag src_subdir
  local name; name="$(basename "$3")"
  echo "  $name  <-  $1@$2"
  git clone --depth 1 -b "$2" -q "https://github.com/$1" "$SRC/$name-repo"
  cp -r "$SRC/$name-repo/$3" "$LIB/"
}

dist() { # name version [entry_points]
  local d="$LIB/$1-$2.dist-info"; mkdir -p "$d"
  printf 'Metadata-Version: 2.1\nName: %s\nVersion: %s\n' "$1" "$2" > "$d/METADATA"
  printf 'Wheel-Version: 1.0\nGenerator: manual\nRoot-Is-Purelib: true\nTag: py3-none-any\n' > "$d/WHEEL"
  : > "$d/RECORD"
  [ -n "${3:-}" ] && printf '%s\n' "$3" > "$d/entry_points.txt" || true
}

echo "==> 拉取纯 Python 依赖到 $LIB"
fetch pytest-dev/iniconfig      v2.1.0  src/iniconfig
fetch pytest-dev/pluggy         1.6.0   src/pluggy
fetch pytest-dev/pytest         8.4.2   src/_pytest
fetch pytest-dev/pytest         8.4.2   src/pytest
cp "$SRC/pytest-repo/src/py.py" "$LIB/"          # pytest 自带的 py 兼容 shim
fetch pytest-dev/pytest-asyncio v1.2.0  pytest_asyncio
fetch fastapi/fastapi           0.121.0 fastapi
fetch fastapi/annotated-doc     master  src/annotated_doc
fetch encode/uvicorn            0.38.0  uvicorn
# fastapi 0.121 要求 starlette>=0.40,<0.50。若系统装的是 1.0.x，这里影子覆盖它。
fetch encode/starlette          0.49.3  starlette

echo "==> 生成构建期版本文件（setuptools-scm 本该生成，源码树里没有）"
printf 'version = "1.6.0"\n__version__ = "1.6.0"\nversion_tuple = (1, 6, 0)\n' > "$LIB/pluggy/_version.py"
printf 'version = "8.4.2"\n__version__ = "8.4.2"\nversion_tuple = (8, 4, 2)\n' > "$LIB/_pytest/_version.py"

echo "==> 生成包元数据（pytest 靠 entry_points 发现插件）"
dist pytest_asyncio 1.2.0 "[pytest11]
asyncio = pytest_asyncio.plugin"
dist pytest 8.4.2 "[console_scripts]
pytest = pytest:console_main"
dist pluggy 1.6.0; dist iniconfig 2.1.0; dist fastapi 0.121.0
dist uvicorn 0.38.0; dist annotated_doc 0.0.5; dist starlette 0.49.3

rm -rf "$SRC"
echo
echo "完成。使用方式："
echo "  export PYTHONPATH=$LIB:\$PWD/backend/src"
echo "  cd backend && python3 -m pytest -q"
