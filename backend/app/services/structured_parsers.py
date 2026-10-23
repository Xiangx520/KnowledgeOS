"""Extract explicit HTML and DOCX structure in source order."""
import re

from app.services.document_structure import ParsedBlock


def table_block(rows: list[list[str]], has_header: bool = True) -> ParsedBlock:
    lines = [" | ".join(cell.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
                        .replace("|", r"\|") for cell in row) for row in rows]
    header = ()
    if lines and has_header:
        separator = " | ".join("---" for _ in rows[0])
        lines.insert(1, separator)
        header = tuple(lines[:2])
    return ParsedBlock("\n".join(lines), "table", header=header)


def html_blocks(text: str) -> tuple[ParsedBlock, ...]:
    from bs4 import BeautifulSoup, Comment, NavigableString

    soup = BeautifulSoup(text, "html.parser")
    for element in soup(["script", "style", "noscript", "head"]):
        element.decompose()
    blocks, prose = [], []

    def flush():
        value = re.sub(r"[ \t\f\v]+", " ", "".join(prose)).strip()
        if value:
            blocks.append(ParsedBlock(value))
        prose.clear()

    def walk(node):
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            prose.append(str(node))
            return
        name = node.name or ""
        if re.fullmatch(r"h[1-6]", name):
            flush()
            blocks.append(ParsedBlock(node.get_text("", strip=False).strip(), "heading", int(name[1])))
        elif name == "table":
            flush()
            rows = [row for row in node.find_all("tr") if row.find_parent("table") is node]
            cells = [[cell.get_text("\n", strip=True) for cell in row.find_all(["td", "th"], recursive=False)] for row in rows]
            if cells:
                blocks.append(table_block(cells, bool(rows[0].find("th", recursive=False))))
        elif name == "pre" or (name == "code" and node.parent.name not in {"p", "li", "span", "a"}):
            flush()
            code = node.find("code") if name == "pre" else node
            language = next((c.removeprefix("language-") for c in (code or node).get("class", [])
                             if c.startswith("language-")), "")
            blocks.append(ParsedBlock(node.get_text(), "code", language=language))
        elif name == "br":
            prose.append("\n")
        else:
            boundary = name in {"p", "div", "section", "article", "li", "ul", "ol", "blockquote", "hr", "main"}
            if boundary:
                flush()
            for child in node.children:
                walk(child)
            if boundary:
                flush()

    walk(soup)
    flush()
    return tuple(blocks)


def docx_blocks(doc) -> tuple[ParsedBlock, ...]:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    blocks, code = [], []

    def flush_code():
        if code:
            blocks.append(ParsedBlock("\n".join(code), "code"))
            code.clear()

    # Iterate body children rather than separate paragraphs/tables collections.
    for element in doc.element.body.iterchildren():
        if element.tag.endswith("}p"):
            paragraph = Paragraph(element, doc)
            style = paragraph.style.name if paragraph.style else ""
            if style.casefold().replace(" ", "") in {"code", "codeblock"}:
                code.append(paragraph.text)
                continue
            flush_code()
            if paragraph.text.strip():
                heading = re.fullmatch(r"Heading\s*([1-9])", style, re.I)
                blocks.append(ParsedBlock(paragraph.text, "heading", int(heading[1])) if heading
                              else ParsedBlock(paragraph.text))
        elif element.tag.endswith("}tbl"):
            flush_code()
            table = Table(element, doc)
            blocks.append(table_block([[cell.text for cell in row.cells] for row in table.rows]))
    flush_code()
    return tuple(blocks)
