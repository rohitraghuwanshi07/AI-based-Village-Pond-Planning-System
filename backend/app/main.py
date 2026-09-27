"""
Village Pond Planning System — API entrypoint.

Run locally with:

    uvicorn app.main:app --reload --port 8000
"""



from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


from app.routers import village, rainfall, terrain, catchment, pond


app = FastAPI(
    title="Village Pond Planning System",
    description="Recommends suitable pond locations using terrain, catchment, and rainfall analysis.",
    version="0.1.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", tags=["system"])
def health_check():
    """Simple liveness check — confirms the server is up."""
    return {
        "status": "ok",
        "service": "village-pond-planner",
    }


app.include_router(village.router)
app.include_router(rainfall.router)
app.include_router(terrain.router)
app.include_router(catchment.router)
app.include_router(pond.router)

