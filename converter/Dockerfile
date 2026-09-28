# Heavy native deps (Pandoc, TeX Live for PDF export) live ONLY in this image.
FROM python:3.12-slim

# pandoc converts docx→gfm (tables, OMML→LaTeX, --extract-media).
# texlive-xetex + DejaVu font for PDF export via xelatex (Cyrillic-safe).
# lmodern + texlive-latex-recommended supply lmodern.sty, which pandoc's default
# LaTeX template loads — xelatex aborts with "lmodern.sty not found" otherwise.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       pandoc \
       texlive-xetex \
       texlive-latex-recommended \
       texlive-fonts-recommended \
       lmodern \
       fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8002
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002"]
