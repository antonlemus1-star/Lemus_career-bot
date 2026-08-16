# ---------------- Извлечение текста из файлов ----------------
def rtf_to_text(raw: str) -> str:
    text = re.sub(
        r"\\'([0-9a-fA-F]{2})",
        lambda m: bytes.fromhex(m.group(1)).decode("cp1251", errors="ignore"),
        raw,
    )
    text = re.sub(r"\\[a-z]+-?\d* ?", " ", text)
    text = re.sub(r"[{}]", "", text)
    return html.unescape(text).strip()


def extract_text(path: str, file_name: str) -> str:
  fn = file_name.lower()
  text_content = ""
  try:
    if fn.endswith(".pdf"):
      reader = PdfReader(path)
      pages_text = []
      for page in reader.pages:
        t = page.extract_text()
        if t:
          pages_text.append(t)
      text_content = "\n".join(pages_text)
    elif fn.endswith(".docx"):
      doc = Document(path)
      text_content = "\n".join(p.text for p in doc.paragraphs if p.text)
    elif fn.endswith(".rtf"):
      with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text_content = rtf_to_text(f.read())
    elif fn.endswith(".txt"):
      with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text_content = f.read()
  except Exception as e:
    log.error("extract_text failed for %s: %s", file_name, e)

  return text_content.strip()
