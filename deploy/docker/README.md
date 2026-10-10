# Docker deployment

The repository-root `Dockerfile` remains the compatibility build entrypoint.
Images must run as UID `10001`, keep runtime state under `/app/data`, and never
copy a local database, cache or log directory into the image.
