# Compose deployment

The compatibility entrypoint remains the repository-root `compose.yaml` so
existing operators can upgrade without changing commands. New deployment
manifests belong in this directory and must mount `/app/data` as the shared
local runtime volume.
