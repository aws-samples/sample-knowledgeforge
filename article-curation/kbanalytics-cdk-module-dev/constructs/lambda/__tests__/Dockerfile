# Test fixture only - referenced by docker-lambda.test.ts via
# DockerImageCode.fromImageAsset(). It is never built or deployed; the USER and
# HEALTHCHECK instructions below exist purely to satisfy container linters.
FROM public.ecr.aws/lambda/python:3.12

# Copy function code
COPY index.py ${LAMBDA_TASK_ROOT}

# Run as a non-root user (least privilege)
USER 1000

# Container liveness check
HEALTHCHECK CMD ["python", "-c", "import sys; sys.exit(0)"]

# Set the CMD to your handler
CMD [ "index.handler" ]
