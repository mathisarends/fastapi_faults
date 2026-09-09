from fastapi import FastAPI

from fastapi_faults import FaultRegistry

from . import api


def create_app() -> FastAPI:
    app = FastAPI(title="fastapi-faults namespaced showcase")
    app.include_router(api.router)

    # The composition root supplies deployment-specific policy once.
    faults = FaultRegistry.merge(
        api.faults,
        name="server",
        type_base="https://api.example.com/problems",
    )
    faults.install(app)
    return app


app = create_app()
