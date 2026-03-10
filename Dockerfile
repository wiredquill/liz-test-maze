FROM registry.suse.com/bci/python:3.11

WORKDIR /app

# Install kubectl for cluster name resolution and llm-config reads
RUN zypper install -y curl tar && \
    KUBECTL_VERSION=$(curl -sL https://dl.k8s.io/release/stable.txt) && \
    curl -sLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && \
    chmod +x /usr/local/bin/kubectl && \
    zypper clean -a

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY app/ .

# Results are persisted on a PVC mounted at /results
VOLUME ["/results"]

# Config (tests.yaml) is mounted as a ConfigMap at /config/tests.yaml
# The app re-reads it on each request, so changes are hot-reloaded without restart.

EXPOSE 8080

ENV RESULTS_DIR=/results \
    CONFIG_PATH=/config/tests.yaml \
    PYTHONUNBUFFERED=1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
