try:
    from media_server_manager.cli.admin import main
except (ImportError, ModuleNotFoundError):
    from src.media_server_manager.cli.admin import main

if __name__ == "__main__":
    raise SystemExit(main())
