"""Ordered, page-preserving structural splitting with a hard character budget."""
from __future__ import annotations

from dataclasses import replace

from app.config import get_settings
from app.services.document_structure import ParsedBlock, ParsedPage, code_text


def bounded_parts(text: str, size: int) -> list[str]:
    """Lossless, non-overlapping fallback, including for a single oversized line."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def line_groups(text: str, size: int) -> list[str]:
    groups, current = [], ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > size:
            groups.append(current)
            current = ""
        while len(line) > size:
            groups.append(line[:size])
            line = line[size:]
        current += line
    if current:
        groups.append(current)
    return groups


class HeadingSplitter:
    def split(self, blocks: tuple[ParsedBlock, ...]) -> list[tuple[ParsedBlock, ...]]:
        sections, current, hierarchy = [], [], []
        for block in blocks:
            if block.kind == "heading":
                if current:
                    sections.append(tuple(current))
                current = []
                while hierarchy and hierarchy[-1].heading_level >= block.heading_level:
                    hierarchy.pop()
                hierarchy.append(block)
            current.append(block)
        if current:
            sections.append(tuple(current))
        return sections


class TableSplitter:
    def __init__(self, chunk_size: int):
        self.chunk_size = chunk_size

    def split(self, block: ParsedBlock) -> list[str]:
        if len(block.text) <= self.chunk_size:
            return [block.text]
        prefix = "\n".join(block.header)
        prefix = prefix + "\n" if prefix else ""
        budget = self.chunk_size - len(prefix)
        if budget < 1:
            return bounded_parts(block.text, self.chunk_size)
        # Only the table header is repeated; data rows occur in exactly one group.
        body = "\n".join(block.text.split("\n")[len(block.header):])
        return [prefix + part for part in line_groups(body, budget)] or bounded_parts(block.text, self.chunk_size)


class CodeBlockSplitter:
    def __init__(self, chunk_size: int):
        self.chunk_size = chunk_size

    def split(self, block: ParsedBlock) -> list[str]:
        rendered = code_text(block)
        if len(rendered) <= self.chunk_size:
            return [rendered]
        # Render with the original body to account for the longest embedded fence.
        overhead = len(rendered) - len(block.text)
        budget = self.chunk_size - overhead
        if budget < 1:
            return bounded_parts(rendered, self.chunk_size)
        return [code_text(replace(block, text=part)) for part in line_groups(block.text, budget)]


def chunk_pages(pages: list[ParsedPage]) -> list[dict]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    settings = get_settings()
    prose_splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", ". ", " ", ""], length_function=len)
    heading_splitter = HeadingSplitter()
    table_splitter = TableSplitter(settings.chunk_size)
    code_splitter = CodeBlockSplitter(settings.chunk_size)
    chunks = []
    for page in pages:
        blocks = page.blocks or (ParsedBlock(page.text),)
        blocks = tuple(replace(b, text=b.text.replace("\x00", ""),
                               header=tuple(h.replace("\x00", "") for h in b.header),
                               language=b.language.replace("\x00", "")) for b in blocks)
        for section in heading_splitter.split(blocks):
            prose = []

            def emit(values):
                for value in values:
                    if value.strip():
                        chunks.append({"text": value, "page": page.page, "ordinal": len(chunks)})

            def flush():
                emit(prose_splitter.split_text("\n\n".join(prose)))
                prose.clear()

            for block in section:
                if block.kind in {"table", "code"}:
                    flush()
                    emit(table_splitter.split(block) if block.kind == "table" else code_splitter.split(block))
                else:
                    prose.append(block.text)
            flush()
    if not chunks:
        raise ValueError("文档没有可检索文本；扫描件请开启 OCR/视觉模型")
    return chunks
