# Systempakker: tesseract for OCR (norsk + engelsk) + biblioteker opencv-python-headless trenger
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-nor \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Kopier og installer Python-avhengigheter først (utnytter Dockers cache-lag - raskere rebuilds)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopier resten av appkoden
COPY . .

# Render setter selv miljøvariabelen $PORT ved kjøretid - vi binder til den, ikke en fast port
# VIKTIG: hvis filnavnet ditt inneholder mellomrom (f.eks. "Lager app.py"), MÅ det stå i anførselstegn slik som her.
# Anbefaling: bytt filnavn til noe uten mellomrom (f.eks. lager_app.py) for å unngå fallgruver senere.
CMD streamlit run "Lager app.py" \
    --server.port=$PORT \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --server.enableCORS=false \
    --server.enableXsrfProtection=false
