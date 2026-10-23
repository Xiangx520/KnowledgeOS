"""Internal page structure shared by format parsers and splitters."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    kind: Literal["text", "heading", "table", "code"] = "text"
    heading_level: int = 0
    header: tuple[str, ...] = ()
    language: str = ""


@dataclass(frozen=True)
class ParsedPage:
    text: str
    page: int = 1
    blocks: tuple[ParsedBlock, ...] = ()


def fence_open(line: str):
    match = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$", line.rstrip("\r\n"))
    if match and not (match[1][0] == "`" and "`" in match[2]):
        return match[1], match[2].strip()
    return None


def fence_close(line: str, fence: str) -> bool:
    return bool(re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", line.rstrip("\r\n")))


def table_cells(line: str) -> list[str]:
    # Escaped pipes belong to a cell, not to the column delimiter.
    return re.split(r"(?<!\\)\|", line.strip().strip("|"))


def table_separator(line: str) -> bool:
    cells = table_cells(line)
    return "|" in line and bool(cells) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", c) for c in cells)


def markdown_blocks(text: str) -> tuple[ParsedBlock, ...]:
    """Recognize fenced code before interpreting any Markdown within it."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    blocks, prose = [], []

    def flush():
        if prose:
            value = "".join(prose).strip()
            if value:
                blocks.append(ParsedBlock(value))
            prose.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        opening = fence_open(line)
        if opening:
            flush()
            fence, language = opening
            index += 1
            body = []
            while index < len(lines) and not fence_close(lines[index], fence):
                body.append(lines[index])
                index += 1
            blocks.append(ParsedBlock("".join(body), "code", language=language))
            if index < len(lines):
                index += 1
            continue
        heading = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$", line.rstrip("\n"))
        if heading:
            flush()
            blocks.append(ParsedBlock(line.strip(), "heading", len(heading[1])))
        elif (line.strip() and index + 1 < len(lines)
              and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*\n?", lines[index + 1])):
            flush()
            level = 1 if lines[index + 1].lstrip().startswith("=") else 2
            blocks.append(ParsedBlock(line.rstrip("\n") + "\n" + lines[index + 1].rstrip("\n"), "heading", level))
            index += 1
        elif ("|" in line and index + 1 < len(lines) and table_separator(lines[index + 1])
              and len(table_cells(line)) == len(table_cells(lines[index + 1]))):
            flush()
            header = (line.rstrip("\n"), lines[index + 1].rstrip("\n"))
            rows = list(header)
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index] and not fence_open(lines[index]):
                if re.match(r"^ {0,3}#{1,6}\s", lines[index]):
                    break
                rows.append(lines[index].rstrip("\n"))
                index += 1
            blocks.append(ParsedBlock("\n".join(rows), "table", header=header))
            continue
        else:
            prose.append(line)
        index += 1
    flush()
    return tuple(blocks)


def code_text(block: ParsedBlock) -> str:
    """Choose a fence that cannot be closed by a line in the code body."""
    longest = max((len(m[0]) for m in re.finditer(r"`+", block.text)), default=0)
    fence = "`" * max(3, longest + 1)
    language = block.language.replace("`", "").replace("\n", " ").replace("\r", " ")
    # Keep the wrapper's newline separate from the body, including a trailing
    # newline that is part of the original code.
    return fence + language + "\n" + block.text + "\n" + fence


def block_text(block: ParsedBlock) -> str:
    return code_text(block) if block.kind == "code" else block.text
