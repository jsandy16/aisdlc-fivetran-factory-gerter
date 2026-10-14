# Pipeline Factory runtime image: one image for Lambda (this CMD) and EKS pods (`python -m factory_runtime.run`).
# Built by scripts/build-runtime-image.sh (context: build/runtime-image/); the generated Terraform and DAGs reference
# it by digest only. Base pinned by digest (AWS Lambda Python 3.12, multi-arch index).
FROM public.ecr.aws/lambda/python:3.12@sha256:c05509c1a303e7ca10acc26782f530ff1a8573411132e2a67c1924d687a65a96

# Third-party dependencies exactly as locked (uv.lock), verified by hash; then the workspace wheels without deps.
COPY requirements.txt wheels/ /tmp/build/
RUN pip install --no-cache-dir --require-hashes -r /tmp/build/requirements.txt --target "${LAMBDA_TASK_ROOT}" \
 && pip install --no-cache-dir --no-deps --target "${LAMBDA_TASK_ROOT}" /tmp/build/*.whl \
 && rm -rf /tmp/build \
 && ln -sf /var/lang/bin/python3 /usr/local/bin/python

# Pods run the same code outside the Lambda runtime: make the task root importable; no data, no secrets in the image.
ENV PYTHONPATH=/var/task PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1000
CMD ["factory_runtime.lambda_handler.handle"]
