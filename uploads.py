# -*- coding: utf-8 -*-
"""회사 문서 업로드: multipart/form-data 파싱과 파일→텍스트 추출을 담당한다."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import List, Optional

MAX_FILE_BYTES = 8 * 1024 * 1024  # 파일당 8MB
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}


@dataclass
class MultipartFile:
    field_name: str
    filename: str
    content_type: str
    data: bytes


def _parse_content_disposition(header_value: str) -> dict:
    parts = {}
    for m in re.finditer(r'([a-zA-Z0-9_-]+)="([^"]*)"', header_value):
        parts[m.group(1)] = m.group(2)
    return parts


def parse_multipart(content_type: str, body: bytes) -> List[MultipartFile]:
    """`multipart/form-data` 본문을 파싱해 파일 파트 목록을 반환한다.

    표준 라이브러리의 `cgi.FieldStorage`는 Python 3.13에서 제거될 예정이라
    쓰지 않고, 우리 프론트가 보내는 형태(브라우저 FormData)만 정확히
    처리하면 되는 최소 파서를 직접 구현한다.
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        raise ValueError("multipart 경계(boundary)를 찾을 수 없습니다.")
    boundary = ("--" + m.group(1)).encode("utf-8")

    files: List[MultipartFile] = []
    for chunk in body.split(boundary)[1:-1]:
        chunk = chunk.strip(b"\r\n")
        if not chunk:
            continue
        header_blob, _, data = chunk.partition(b"\r\n\r\n")
        if not header_blob:
            continue
        headers = {}
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disposition = headers.get("content-disposition", "")
        fields = _parse_content_disposition(disposition)
        filename = fields.get("filename")
        if not filename:
            continue  # 파일이 아닌 일반 필드는 이 업로드 용도에서는 사용하지 않는다.
        data = data.rstrip(b"\r\n")
        files.append(
            MultipartFile(
                field_name=fields.get("name", "file"),
                filename=filename,
                content_type=headers.get("content-type", "application/octet-stream"),
                data=data,
            )
        )
    return files


def extract_text(filename: str, data: bytes) -> str:
    """확장자에 맞는 방식으로 파일에서 텍스트를 추출한다."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"파일이 너무 큽니다 (최대 {MAX_FILE_BYTES // (1024*1024)}MB): {filename}")

    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"지원하지 않는 파일 형식입니다: {filename} (pdf, docx, txt, md만 가능)")

    if ext in (".txt", ".md"):
        return data.decode("utf-8", errors="replace")

    if ext == ".pdf":
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if ext == ".docx":
        import docx

        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)

    raise ValueError(f"지원하지 않는 파일 형식입니다: {filename}")
