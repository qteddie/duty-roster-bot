# The bot runs in an isolated Conda environment named "work".
FROM continuumio/miniconda3:latest

WORKDIR /app

COPY requirements.txt ./

RUN apt-get update \
    && apt-get install --yes --no-install-recommends tesseract-ocr tesseract-ocr-chi-tra \
    && rm -rf /var/lib/apt/lists/* \
    && conda create --yes --name work python=3.11 pip \
    && conda run --no-capture-output --name work python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.3,<3" \
    && conda run --no-capture-output --name work python -m pip install --no-cache-dir -r requirements.txt \
    && conda clean --all --yes

COPY . ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["conda", "run", "--no-capture-output", "--name", "work", "python", "discord_bot.py"]
