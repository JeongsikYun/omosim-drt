"""입출력 유틸.

- JSON 로드/저장(원자적 저장)
- CSV 헤더 작성/행 append
- 대용량 파일 병합(append)
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def read_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: str | Path, obj: Any, indent: Optional[int] = None) -> None:
    """원자적 저장: 임시 파일에 쓴 후 rename. OneDrive 잠금 시 재시도."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    os.close(fd)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
        # os.replace can fail on Windows when OneDrive locks the file
        last_exc: Optional[Exception] = None
        for attempt in range(10):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.5)
        raise last_exc
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def write_csv_header(path: str | Path, header: List[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)


def append_csv_row(path: str | Path, row: List[Any]) -> None:
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)


def append_csv_rows(path: str | Path, rows: Iterable[List[Any]]) -> None:
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(row)


def append_file(
    dst_path: str | Path,
    src_path: str | Path,
    skip_header: bool = False,
    retries: int = 8,
    delay_sec: float = 0.25,
) -> None:
    """src 파일 내용을 dst에 append.

    - src와 dst 모두 텍스트 파일(CSV)라고 가정
    - skip_header=True면 src의 첫 줄을 버림
    """
    dst_path = Path(dst_path)
    src_path = Path(src_path)
    if not src_path.exists():
        return
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if dst_path.resolve() == src_path.resolve():
            return
    except OSError:
        pass

    last_exc: Optional[Exception] = None
    for _ in range(retries):
        try:
            with open(src_path, "r", encoding="utf-8", newline="") as src, open(
                dst_path, "a", encoding="utf-8", newline=""
            ) as dst:
                if skip_header:
                    _ = src.readline()
                for line in src:
                    dst.write(line)
            return
        except (OSError, PermissionError) as exc:
            last_exc = exc
            time.sleep(delay_sec)

    if last_exc is not None:
        raise last_exc


def now_stamp() -> str:
    """YYYYMMDD_HHMMSS (Asia/Seoul 표준)
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Seoul")
    return datetime.now(tz=tz).strftime("%Y%m%d_%H%M%S")
