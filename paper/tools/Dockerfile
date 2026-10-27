ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer python3-docx python3-matplotlib python3-yaml \
    fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
ENV MPLBACKEND=Agg PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
