FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INSIGHTFACE_ROOT=/models \
    PHOTO_DIR=/data/photos \
    MODEL_NAME=buffalo_l \
    DET_SIZE=640

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && (pip uninstall -y opencv-python || true) \
    && pip install --no-cache-dir "opencv-python-headless==4.10.0.84" \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Bake the face model into the image so the API and the importer do not download it on first use.
RUN python - <<'PY'
import time
from insightface.app import FaceAnalysis

last_error = None
for attempt in range(1, 4):
    try:
        FaceAnalysis(
            name="buffalo_l",
            root="/models",
            providers=["CPUExecutionProvider"],
            allowed_modules=["detection", "recognition"],
        ).prepare(ctx_id=-1, det_size=(640, 640))
        print("model ready", flush=True)
        break
    except Exception as exc:
        last_error = exc
        print(f"model prepare failed on attempt {attempt}: {exc}", flush=True)
        time.sleep(5)
else:
    raise SystemExit(last_error)
PY

# Bake the license-plate detector and OCR models into the same image.
RUN python - <<'PY'
import numpy as np
from fast_alpr import ALPR

alpr = ALPR(
    detector_model="yolo-v9-t-384-license-plate-end2end",
    ocr_model="cct-xs-v2-global-model",
    ocr_device="cpu",
    detector_providers=["CPUExecutionProvider"],
    ocr_providers=["CPUExecutionProvider"],
)
alpr.predict(np.zeros((64, 64, 3), dtype=np.uint8))
print("plate model ready", flush=True)
PY

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

RUN mkdir -p /data/photos

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
