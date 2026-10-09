import packages.infrastructure.queue.tasks  # noqa: F401
from packages.infrastructure.queue.celery_app import celery_app


def main() -> None:
    celery_app.start(["beat", "--loglevel=INFO"])


if __name__ == "__main__":
    main()
