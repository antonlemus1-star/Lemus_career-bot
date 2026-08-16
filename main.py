def rtf_to_text(raw: str) -> str:
    # Очищаем RTF от тегов форматирования и шрифтов
    text = re.sub(r"\\par[d]?", "\n", raw)
    text = re.sub(r"\\f\d+", "", text)
    text = re.sub(r"\\[a-z]+-?\d* ?([-?\d+])?", "", text)
    text = re.sub(r"[{}]", "", text)
    # Декодируем escape-последовательности кириллицы
    text = re.sub(r"\\'[0-9a-fA-F]{2}", 
                  lambda m: bytes.fromhex(m.group(1)).decode("cp1251", errors="ignore"), text)
    return html.unescape(text).strip()

def extract_text(path: str, file_name: str) -> str:
    fn = file_name.lower()
    try:
        if fn.endswith(".pdf"):
            return "".join(p.extract_text() or "" for p in PdfReader(path).pages)
        elif fn.endswith(".docx"):
            return "\n".join(p.text for p in Document(path).paragraphs)
        elif fn.endswith(".doc"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                clean = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', content)
                return " ".join(clean.split())
        elif fn.endswith(".rtf"):
            with open(path, "r", encoding="latin1", errors="ignore") as f:
                return rtf_to_text(f.read())
        elif fn.endswith(".txt"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        log.error("extract_text failed: %s", e)
    return ""