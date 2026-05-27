import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pb2craft import __version__
from pb2craft.app import AppContainer, seed_from_env
from pb2craft.config import Settings
from pb2craft.web.routes import router as web_router


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Read settings fresh here so tests / runtime env changes take effect.
    settings = Settings()
    _configure_logging(settings.log_level)
    log = logging.getLogger("pb2craft")
    log.info("Starting PocketBook2Craft v%s", __version__)
    log.info("Config dir: %s", settings.config_dir)

    container = AppContainer(settings=settings)
    seed_from_env(container)
    app.state.container = container
    container.start_scheduler()

    yield

    container.shutdown_scheduler()
    log.info("Shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="PocketBook2Craft",
        version=__version__,
        lifespan=lifespan,
    )
    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(web_router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "pb2craft.main:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
