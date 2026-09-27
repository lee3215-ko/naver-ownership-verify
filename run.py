"""진입점."""

from paths import APP_VERSION, init_runtime_paths

init_runtime_paths()

from app import App  # noqa: E402


def main() -> None:
    App().run()


if __name__ == "__main__":
    main()
